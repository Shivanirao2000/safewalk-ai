import asyncio
import functools
import logging
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

ASCENDING = 1  # pymongo.ASCENDING; defined here to avoid unresolved-import warnings

from config import settings
from models import SafeWalkSession

logger = logging.getLogger(__name__)

DB_NAME = "safewalk"
COLLECTION = "sessions"

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF  = 0.4   # seconds; multiplied by attempt number


# ------------------------------------------------------------------ #
#  Retry decorator for CRUD methods                                   #
# ------------------------------------------------------------------ #
def _retryable(method):
    """Retry a MongoService coroutine method up to _RETRY_ATTEMPTS times."""
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return await method(self, *args, **kwargs)
            except Exception as exc:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error(
                        "%s exhausted all %d retries: %s",
                        method.__name__, _RETRY_ATTEMPTS, exc,
                    )
                    raise
                wait = _RETRY_BACKOFF * attempt
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    method.__name__, attempt, _RETRY_ATTEMPTS, exc, wait,
                )
                await asyncio.sleep(wait)
    return wrapper


class MongoService:
    def __init__(self):
        self.client: AsyncIOMotorClient | None = None
        self._collection: AsyncIOMotorCollection | None = None

    @property
    def collection(self) -> AsyncIOMotorCollection:
        if self._collection is None:
            raise RuntimeError("MongoService.connect() has not been called")
        return self._collection

    @property
    def _failed_alerts(self) -> AsyncIOMotorCollection:
        return self.client[DB_NAME]["failed_alerts"]

    async def connect(self):
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                self.client = AsyncIOMotorClient(
                    settings.mongo_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                    maxPoolSize=10,
                    minPoolSize=2,
                )
                await self.client.admin.command("ping")
                db = self.client[DB_NAME]
                self._collection = db[COLLECTION]
                await self._collection.create_index(
                    [("session_id", ASCENDING)], unique=True, background=True
                )
                await self._collection.create_index(
                    [("status", ASCENDING)], background=True
                )
                await self._collection.create_index(
                    [("distress_level", ASCENDING), ("alert_sent", ASCENDING)],
                    background=True,
                )
                logger.info(
                    "Connected to MongoDB (db=%s, collection=%s, pool=2–10)",
                    DB_NAME, COLLECTION,
                )
                return
            except Exception as exc:
                if attempt == _RETRY_ATTEMPTS:
                    logger.exception("MongoDB connect failed after %d attempts: %s", attempt, exc)
                    raise
                wait = _RETRY_BACKOFF * attempt
                logger.warning(
                    "MongoDB connect failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, _RETRY_ATTEMPTS, exc, wait,
                )
                await asyncio.sleep(wait)

    async def disconnect(self):
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")

    # ------------------------------------------------------------------ #
    #  CRUD                                                               #
    # ------------------------------------------------------------------ #

    @_retryable
    async def create_session(self, session: SafeWalkSession) -> str:
        doc = session.model_dump(mode="json")
        await self.collection.insert_one(doc)
        logger.debug("Created session %s", session.session_id)
        return session.session_id

    @_retryable
    async def get_session(self, session_id: str) -> SafeWalkSession | None:
        doc = await self.collection.find_one(
            {"session_id": session_id}, projection={"_id": False}
        )
        if doc is None:
            return None
        return SafeWalkSession.model_validate(doc)

    @_retryable
    async def update_session(self, session_id: str, updates: dict) -> bool:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = await self.collection.update_one(
            {"session_id": session_id},
            {"$set": updates},
        )
        if result.matched_count == 0:
            logger.warning("update_session: session %s not found", session_id)
        return result.matched_count > 0

    @_retryable
    async def append_transcript(self, session_id: str, entry: dict) -> bool:
        """Push a single {timestamp, speaker, text} entry onto the transcript array."""
        result = await self.collection.update_one(
            {"session_id": session_id},
            {
                "$push": {"transcript": entry},
                "$set": {"updated_at": datetime.now(timezone.utc).isoformat()},
            },
        )
        if result.matched_count == 0:
            logger.warning("append_transcript: session %s not found", session_id)
        return result.matched_count > 0

    async def mark_alert_sent(self, session_id: str) -> bool:
        return await self.update_session(
            session_id, {"alert_sent": True, "status": "escalated"}
        )

    @_retryable
    async def get_all_active_sessions(self) -> list[SafeWalkSession]:
        cursor = self.collection.find(
            {"status": "active"}, projection={"_id": False}
        )
        return [SafeWalkSession.model_validate(doc) async for doc in cursor]

    # ------------------------------------------------------------------ #
    #  Resilience helpers                                                 #
    # ------------------------------------------------------------------ #

    @_retryable
    async def get_stale_high_distress_sessions(
        self, distress_threshold: int = 4, stale_minutes: int = 2
    ) -> list[SafeWalkSession]:
        """Return active sessions with high distress that haven't been updated recently."""
        threshold_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        ).isoformat()
        cursor = self.collection.find(
            {
                "status": "active",
                "alert_sent": False,
                "distress_level": {"$gte": distress_threshold},
                "updated_at": {"$lt": threshold_ts},
            },
            projection={"_id": False},
        )
        return [SafeWalkSession.model_validate(doc) async for doc in cursor]

    @_retryable
    async def log_failed_alert(self, session_id: str, error: str) -> None:
        """Persist a failed SMS attempt so it can be retried externally."""
        await self._failed_alerts.insert_one(
            {
                "session_id": session_id,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "retried": False,
            }
        )
        logger.info("Logged failed alert for session %s", session_id)


mongo_service = MongoService()
