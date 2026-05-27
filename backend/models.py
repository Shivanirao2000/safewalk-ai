from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from datetime import datetime, timezone
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LocationUpdate(BaseModel):
    lat: float
    lng: float
    address: str


class SafeWalkSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_name: str
    emergency_contact: str  # email address, e.g. contact@example.com
    location: LocationUpdate
    status: Literal["active", "safe", "alert", "escalated", "alert_failed"] = "active"
    distress_level: int = Field(default=0, ge=0, le=5)  # 0=calm, 5=emergency
    transcript: list[dict] = Field(default_factory=list)  # [{timestamp, speaker, text}]
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    alert_sent: bool = False

    @field_validator("emergency_contact")
    @classmethod
    def must_be_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("emergency_contact must be a valid email address")
        return v


class SessionCreateRequest(BaseModel):
    user_name: str
    emergency_contact: str  # email address
    location: LocationUpdate


class StatusUpdateRequest(BaseModel):
    status: Literal["active", "safe", "alert", "escalated", "alert_failed"]


class EmergencyAlert(BaseModel):
    session_id: str
    message: str
    location: LocationUpdate
    contact_number: str


class EmergencyTriggerRequest(BaseModel):
    session_id: str
    manual: bool = False


class EmergencyCancelRequest(BaseModel):
    session_id: str


class CheckIn(BaseModel):
    session_id: str
    location: Optional[LocationUpdate] = None
    message: Optional[str] = None


class AIMessage(BaseModel):
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime = Field(default_factory=_now)
