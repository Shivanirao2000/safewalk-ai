from datetime import timezone, datetime

from fastapi import APIRouter, HTTPException

from models import LocationUpdate, SafeWalkSession, SessionCreateRequest, StatusUpdateRequest
from services.mongo_service import mongo_service

router = APIRouter()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _get_or_404(session_id: str) -> SafeWalkSession:
    session = await mongo_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.post("/create", status_code=201)
async def create_session(body: SessionCreateRequest):
    session = SafeWalkSession(
        user_name=body.user_name,
        emergency_contact=body.emergency_contact,
        location=body.location,
    )
    await mongo_service.create_session(session)
    return {"session_id": session.session_id, "status": session.status}


@router.get("/{session_id}", response_model=SafeWalkSession)
async def get_session(session_id: str):
    return await _get_or_404(session_id)


@router.patch("/{session_id}/location", response_model=SafeWalkSession)
async def update_location(session_id: str, body: LocationUpdate):
    await _get_or_404(session_id)
    updated = await mongo_service.update_session(
        session_id,
        {"location": body.model_dump()},
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Location update failed")
    return await _get_or_404(session_id)


@router.patch("/{session_id}/status", response_model=SafeWalkSession)
async def update_status(session_id: str, body: StatusUpdateRequest):
    await _get_or_404(session_id)
    updated = await mongo_service.update_session(session_id, {"status": body.status})
    if not updated:
        raise HTTPException(status_code=500, detail="Status update failed")
    return await _get_or_404(session_id)


@router.delete("/{session_id}/end")
async def end_session(session_id: str):
    await _get_or_404(session_id)
    await mongo_service.update_session(
        session_id,
        {
            "status": "safe",
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"message": "Session ended safely"}
