import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.elevenlabs.io/v1"

_AGENT_CONFIG = {
    "prompt": (
        "You are SafeWalk, a real-time voice safety companion. Your job is to keep the user "
        "calm and safe during their walk. Ask gentle check-in questions, notice signs of "
        "distress, and keep them talking. If they seem scared, be reassuring. Never reveal "
        "you are monitoring for safety - just be a friendly companion. If the user says a "
        "code word like 'purple elephant' or 'I forgot my keys', treat this as a hidden "
        "distress signal."
    ),
    "first_message": "Hey! I'm here with you on your walk. How are you feeling tonight?",
    "language": "en",
}


class ElevenLabsService:
    def __init__(self):
        self._headers = {
            "xi-api-key": settings.elevenlabs_api_key,
        }

    async def get_signed_url(self, agent_id: str) -> str:
        """Request a short-lived signed WebSocket URL for a Conversational AI session."""
        url = f"{_BASE_URL}/convai/conversation/get_signed_url"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                url,
                params={"agent_id": agent_id},
                headers=self._headers,
            )
            response.raise_for_status()
            signed_url: str = response.json()["signed_url"]
            logger.info("Obtained signed URL for agent %s", agent_id)
            return signed_url

    @staticmethod
    def get_agent_config() -> dict:
        """Return the SafeWalk agent prompt configuration."""
        return _AGENT_CONFIG.copy()


elevenlabs_service = ElevenLabsService()
