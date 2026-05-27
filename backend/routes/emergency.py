import logging

from fastapi import APIRouter, HTTPException

from models import EmergencyCancelRequest, EmergencyTriggerRequest, SafeWalkSession
from services.mongo_service import mongo_service
from services.twilio_service import twilio_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _get_session_or_404(session_id: str) -> SafeWalkSession:
    session = await mongo_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.post("/trigger")
async def trigger_emergency(body: EmergencyTriggerRequest):
    session = await _get_session_or_404(body.session_id)

    if session.alert_sent:
        logger.info(
            "Emergency trigger skipped — alert already sent for session %s",
            body.session_id,
        )
        return {
            "success": True,
            "alert_sent": True,
            "message": "Alert was already sent for this session",
        }

    sms_result = await twilio_service.send_emergency_sms(session)

    trigger_source = "manual" if body.manual else "automatic"

    if sms_result["success"]:
        await mongo_service.update_session(
            body.session_id,
            {"status": "escalated", "alert_sent": True, "distress_level": 5},
        )
        logger.warning(
            "Emergency triggered (%s) for session %s — user=%s sms_sid=%s",
            trigger_source, body.session_id, session.user_name, sms_result["message_sid"],
        )
        return {
            "success": True,
            "alert_sent": True,
            "message": f"Emergency alert sent to {session.emergency_contact}",
        }
    else:
        # SMS failed: record for retry, do NOT set alert_sent so a retry is possible
        error_detail = sms_result.get("error", "unknown error")
        await mongo_service.update_session(
            body.session_id,
            {"status": "alert_failed", "distress_level": 5},
        )
        await mongo_service.log_failed_alert(body.session_id, error_detail)
        logger.error(
            "Emergency SMS failed (%s) for session %s — user=%s error=%s — logged for retry",
            trigger_source, body.session_id, session.user_name, error_detail,
        )
        return {
            "success": False,
            "alert_sent": False,
            "error": "SMS failed, logged for retry",
        }


@router.post("/cancel")
async def cancel_emergency(body: EmergencyCancelRequest):
    session = await _get_session_or_404(body.session_id)

    await mongo_service.update_session(body.session_id, {"status": "safe"})

    if session.alert_sent:
        sms_result = await twilio_service.send_safe_confirmation(session)
        logger.info(
            "Safe confirmation sent for session %s — sms_success=%s sms_sid=%s",
            body.session_id,
            sms_result["success"],
            sms_result["message_sid"],
        )

    return {"success": True}


@router.get("/status/{session_id}")
async def emergency_status(session_id: str):
    session = await _get_session_or_404(session_id)
    return {
        "session_id": session.session_id,
        "status": session.status,
        "distress_level": session.distress_level,
        "alert_sent": session.alert_sent,
    }
