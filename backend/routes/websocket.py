import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from config import settings
from models import LocationUpdate, SafeWalkSession
from services.elevenlabs_service import elevenlabs_service
from services.gemini_service import gemini_service
from services.mongo_service import mongo_service
from services.twilio_service import twilio_service

logger = logging.getLogger(__name__)

router = APIRouter()

# session_id → active WebSocket
_connections: dict[str, WebSocket] = {}

# Sessions whose ElevenLabs voice connection has dropped; Gemini fills in as
# text companion so the session continues rather than going silent.
_text_mode_sessions: set[str] = set()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_active_session_or_raise(session_id: str) -> SafeWalkSession:
    session = await mongo_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    if session.status not in ("active", "alert"):
        raise HTTPException(
            status_code=409,
            detail=f"Session '{session_id}' is not active (status={session.status})",
        )
    return session


async def _do_trigger_emergency(session: SafeWalkSession, classification: dict) -> None:
    """Fire-and-continue emergency: send SMS then mark in DB regardless of SMS outcome."""
    sms_result = await twilio_service.send_emergency_sms(session)

    if sms_result["success"]:
        await mongo_service.mark_alert_sent(session.session_id)
        logger.warning(
            "Emergency triggered for session %s (level=%d) — SMS sent",
            session.session_id, classification["distress_level"],
        )
    else:
        # SMS failed mid-session — log for retry, don't set alert_sent so the
        # watchdog or a manual retry can still deliver the alert.
        await mongo_service.update_session(
            session.session_id, {"status": "alert_failed", "distress_level": 5}
        )
        await mongo_service.log_failed_alert(
            session.session_id, sms_result.get("error", "unknown")
        )
        logger.error(
            "Emergency SMS failed for session %s — logged for retry",
            session.session_id,
        )


# ------------------------------------------------------------------
# REST — signed URL
# ------------------------------------------------------------------

@router.get("/elevenlabs/signed-url")
async def get_signed_url(session_id: str = Query(..., description="Active session ID")):
    await _get_active_session_or_raise(session_id)
    signed_url = await elevenlabs_service.get_signed_url(settings.elevenlabs_agent_id)
    return {"signed_url": signed_url, "session_id": session_id}


# ------------------------------------------------------------------
# WebSocket
# ------------------------------------------------------------------

@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    session = await mongo_service.get_session(session_id)
    if session is None or session.status not in ("active", "alert"):
        await websocket.close(code=4004, reason="Session not found or not active")
        return

    await websocket.accept()
    _connections[session_id] = websocket
    logger.info("WebSocket connected: session=%s", session_id)

    try:
        await websocket.send_json({"type": "connected", "session_id": session_id})

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            # ---- ping ------------------------------------------------
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            # ---- client syncing distress level before emergency trigger --
            elif msg_type == "distress_update":
                level = max(0, min(5, int(data.get("level", 0))))
                await mongo_service.update_session(session_id, {"distress_level": level})
                logger.info("Session %s distress synced to %d by client", session_id, level)

            # ---- ElevenLabs voice dropped — switch to text-only mode --
            elif msg_type == "voice_dropped":
                _text_mode_sessions.add(session_id)
                logger.info(
                    "Session %s switched to text-only mode (ElevenLabs dropped)", session_id
                )
                await websocket.send_json({
                    "type": "mode_change",
                    "mode": "text_only",
                    "message": "Voice connection lost — SafeWalk is still with you in text mode.",
                })

            # ---- transcript entry ------------------------------------
            elif msg_type == "transcript":
                speaker: str = data.get("speaker", "user")
                text: str = data.get("text", "")
                entry = {"timestamp": _now_iso(), "speaker": speaker, "text": text}

                await mongo_service.append_transcript(session_id, entry)

                if speaker == "user" and text.strip():
                    fresh = await mongo_service.get_session(session_id)
                    transcript = fresh.transcript if fresh else []

                    classification = await gemini_service.classify_distress(
                        transcript=transcript,
                        latest_message=text,
                        session_id=session_id,
                    )
                    level: int = classification["distress_level"]

                    if level >= 3:
                        await mongo_service.update_session(
                            session_id, {"distress_level": level}
                        )

                    if level >= 4 and fresh and not fresh.alert_sent:
                        await _do_trigger_emergency(fresh, classification)

                    await websocket.send_json({
                        "type": "distress_update",
                        "level": level,
                        "recommend_alert": classification["recommend_alert"],
                        "reasoning": classification["reasoning"],
                        "keywords_detected": classification["keywords_detected"],
                    })

                    # Text-only mode: Gemini acts as a companion since ElevenLabs is gone
                    if session_id in _text_mode_sessions:
                        try:
                            reply = await gemini_service.chat(
                                session_id=session_id, user_message=text
                            )
                            await mongo_service.append_transcript(
                                session_id,
                                {"timestamp": _now_iso(), "speaker": "agent", "text": reply},
                            )
                            await websocket.send_json({
                                "type": "text_reply",
                                "text": reply,
                                "mode": "text_only",
                            })
                        except Exception as exc:
                            logger.exception(
                                "Gemini text-mode reply failed for session %s: %s",
                                session_id, exc,
                            )

            # ---- location update -------------------------------------
            elif msg_type == "location_update":
                try:
                    location = LocationUpdate(
                        lat=data["lat"],
                        lng=data["lng"],
                        address=data.get("address", ""),
                    )
                    await mongo_service.update_session(
                        session_id, {"location": location.model_dump()}
                    )
                except (KeyError, ValueError) as exc:
                    await websocket.send_json(
                        {"type": "error", "detail": f"Invalid location payload: {exc}"}
                    )

            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"Unknown message type: {msg_type!r}"}
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: session=%s", session_id)
    except Exception as exc:
        logger.exception("Unexpected WebSocket error for session %s: %s", session_id, exc)
        try:
            await websocket.send_json({"type": "error", "detail": "Internal server error"})
        except Exception:
            pass
    finally:
        _connections.pop(session_id, None)
        _text_mode_sessions.discard(session_id)
        await mongo_service.update_session(session_id, {"updated_at": _now_iso()})
