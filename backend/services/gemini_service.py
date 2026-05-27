import json
import logging
import re
import time

from google import genai
from google.genai import types

from config import settings

logger = logging.getLogger(__name__)

_MODEL  = "gemini-2.0-flash"
_client = genai.Client(api_key=settings.gemini_api_key)

_COMPANION_SYSTEM_PROMPT = """\
You are SafeWalk AI, a compassionate and calm personal safety companion.
Your role is to keep users company while they walk alone, help them feel safe, and alert them
to any concerns they share. You can give navigation tips, engage in friendly conversation,
and remind users to check in. If a user seems distressed, acknowledge their feelings and
encourage them to trigger an emergency alert if needed. Keep responses concise and reassuring."""

_CLASSIFIER_SYSTEM_PROMPT = """\
You are a safety AI analyzing voice transcripts for signs of distress or danger.
Be sensitive to subtle cues like hesitation, code words, and contextual danger signals.
Always err on the side of caution."""

_DISTRESS_SCALE = """\
Distress scale:
  0 = calm/normal
  1 = mild anxiety
  2 = nervous/concerned
  3 = distressed
  4 = high distress / possible danger
  5 = emergency / immediate help needed"""

_SAFE_DEFAULTS: dict = {
    "distress_level": 0,
    "reasoning": "Unable to classify — defaulting to safe",
    "keywords_detected": [],
    "recommend_alert": False,
}


class GeminiService:
    _RATE_LIMIT_SECS = 10

    def __init__(self):
        self._chats: dict = {}           # session_id -> AsyncChat
        self._last_called: dict = {}     # session_id -> float (time.monotonic)
        self._last_result: dict = {}     # session_id -> last classification dict

    # ------------------------------------------------------------------
    # Companion chat
    # ------------------------------------------------------------------

    def _get_chat(self, session_id: str):
        if session_id not in self._chats:
            self._chats[session_id] = _client.aio.chats.create(
                model=_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=_COMPANION_SYSTEM_PROMPT,
                ),
            )
        return self._chats[session_id]

    async def chat(self, session_id: str, user_message: str) -> str:
        chat = self._get_chat(session_id)
        response = await chat.send_message(user_message)
        return response.text

    def clear_session(self, session_id: str):
        self._chats.pop(session_id, None)
        self._last_called.pop(session_id, None)
        self._last_result.pop(session_id, None)

    # ------------------------------------------------------------------
    # Distress classification
    # ------------------------------------------------------------------

    def _build_classification_prompt(
        self, transcript: list[dict], latest_message: str
    ) -> str:
        recent = transcript[-5:] if len(transcript) > 5 else transcript
        context_lines = "\n".join(
            f"  [{entry.get('timestamp', '?')}] {entry.get('speaker', '?')}: {entry.get('text', '')}"
            for entry in recent
        )
        context_block = context_lines if context_lines else "  (no prior transcript)"

        return f"""\
Analyze the safety of the following conversation transcript and latest message.

{_DISTRESS_SCALE}

Recent transcript (last {len(recent)} entries):
{context_block}

Latest message: "{latest_message}"

Respond with ONLY valid JSON — no markdown, no prose, no code fences:
{{
  "distress_level": <integer 0-5>,
  "reasoning": "<brief explanation>",
  "keywords_detected": ["<word>", ...],
  "recommend_alert": <true|false>
}}"""

    @staticmethod
    def _parse_classification(raw: str) -> dict:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(cleaned)

        distress_level = int(data.get("distress_level", 0))
        if not (0 <= distress_level <= 5):
            raise ValueError(f"distress_level out of range: {distress_level}")

        return {
            "distress_level": distress_level,
            "reasoning": str(data.get("reasoning", "")),
            "keywords_detected": list(data.get("keywords_detected", [])),
            "recommend_alert": bool(data.get("recommend_alert", False)),
        }

    async def classify_distress(
        self, transcript: list[dict], latest_message: str, session_id: str = ""
    ) -> dict:
        now = time.monotonic()
        last = self._last_called.get(session_id, 0.0)
        if session_id and (now - last) < self._RATE_LIMIT_SECS:
            logger.debug(
                "classify_distress skipped for session %s (%.1fs since last call)",
                session_id, now - last,
            )
            return self._last_result.get(session_id, _SAFE_DEFAULTS).copy()

        prompt = self._build_classification_prompt(transcript, latest_message)
        try:
            response = await _client.aio.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_CLASSIFIER_SYSTEM_PROMPT,
                ),
            )
            result = self._parse_classification(response.text)
            if session_id:
                self._last_called[session_id] = now
                self._last_result[session_id] = result
            logger.info(
                "Distress classification — level=%d recommend_alert=%s keywords=%s reason=%r",
                result["distress_level"],
                result["recommend_alert"],
                result["keywords_detected"],
                result["reasoning"],
            )
            return result
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("Failed to parse distress classification response: %s", exc)
            return _SAFE_DEFAULTS.copy()
        except Exception as exc:
            logger.exception("Gemini classify_distress error: %s", exc)
            return _SAFE_DEFAULTS.copy()


gemini_service = GeminiService()
