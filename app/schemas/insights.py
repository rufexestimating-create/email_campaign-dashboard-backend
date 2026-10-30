from datetime import date
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.services.send_time import DEFAULT_TIMEZONE


class SendPlanRequest(BaseModel):
    recipients: list[str] = Field(..., min_length=1, max_length=5000)
    days: int = Field(180, ge=7, le=730)            # how far back to learn from
    timezone: str = DEFAULT_TIMEZONE                 # assumed for contacts with no ZIP, state or timezone


class WarmupUpdate(BaseModel):
    enabled: Optional[bool] = None
    start_date: Optional[date] = None
    seeds_enabled: Optional[bool] = None     # add seed inboxes to every campaign


class SeedCreate(BaseModel):
    email: EmailStr
