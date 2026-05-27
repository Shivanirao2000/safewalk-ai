import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from services.mongo_service import mongo_service
from services.twilio_service import twilio_service
from routes.session import router as session_router
from routes.websocket import router as websocket_router
from routes.emergency import router as emergency_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


_WATCHDOG_INTERVAL = 30   # seconds between each check
_WATCHDOG_DISTRESS  = 4   # minimum distress level that triggers auto-escalation
_WATCHDOG_STALE_MIN = 2   # minutes of inactivity before a session is considered stale


async def _watchdog() -> None:
    """
    Runs every 30 s. Finds sessions that have high distress but whose WebSocket
    dropped before the alert was delivered, and auto-triggers the emergency flow.
    """
    logger.info("Watchdog started (interval=%ds, threshold=%d/5, stale=%dm)",
                _WATCHDOG_INTERVAL, _WATCHDOG_DISTRESS, _WATCHDOG_STALE_MIN)
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            stale = await mongo_service.get_stale_high_distress_sessions(
                distress_threshold=_WATCHDOG_DISTRESS,
                stale_minutes=_WATCHDOG_STALE_MIN,
            )
            for session in stale:
                logger.warning(
                    "Watchdog: auto-escalating session %s "
                    "(distress=%d, alert_sent=False, stale>%dm)",
                    session.session_id, session.distress_level, _WATCHDOG_STALE_MIN,
                )
                sms_result = await twilio_service.send_emergency_sms(session)
                if sms_result["success"]:
                    await mongo_service.mark_alert_sent(session.session_id)
                    logger.warning(
                        "Watchdog: emergency SMS delivered for session %s (SID %s)",
                        session.session_id, sms_result["message_sid"],
                    )
                else:
                    await mongo_service.update_session(
                        session.session_id, {"status": "alert_failed"}
                    )
                    await mongo_service.log_failed_alert(
                        session.session_id, sms_result.get("error", "unknown")
                    )
                    logger.error(
                        "Watchdog: emergency SMS failed for session %s — logged for retry",
                        session.session_id,
                    )
        except asyncio.CancelledError:
            logger.info("Watchdog stopped")
            raise
        except Exception as exc:
            # Log and keep the loop alive — a watchdog that crashes is no watchdog at all
            logger.exception("Watchdog iteration error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — connecting to MongoDB…")
    await mongo_service.connect()
    logger.info("MongoDB connected")
    watchdog_task = asyncio.create_task(_watchdog(), name="safewalk-watchdog")
    yield
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down — closing MongoDB connection…")
    await mongo_service.disconnect()


app = FastAPI(
    title="SafeWalk AI",
    version="1.0.0",
    description="Real-time personal safety companion",
    lifespan=lifespan,
)

# ------------------------------------------------------------------
# CORS — open for all origins in development
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Global exception handler
# ------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)},
    )

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
app.include_router(session_router, prefix="/api/sessions", tags=["sessions"])
app.include_router(emergency_router, prefix="/api/emergency", tags=["emergency"])
# websocket_router owns its own path prefixes (/ws/... and /elevenlabs/...)
app.include_router(websocket_router, prefix="/api", tags=["websocket"])

# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
@app.get("/health", tags=["meta"])
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

# ------------------------------------------------------------------
# Static frontend — mounted last so API routes take priority
# ------------------------------------------------------------------
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
