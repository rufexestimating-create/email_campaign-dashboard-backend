from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class ReplyCreate(BaseModel):
    recipient: EmailStr
    replied_at: Optional[datetime] = None  # when they replied; defaults to now
    campaign_id: Optional[str] = None      # narrows which email they replied to; otherwise the latest one


class PlacementLabelCreate(BaseModel):
    recipient: EmailStr                    # the seed inbox (or contact) the email landed with
    placement: Literal["inbox", "promotions", "spam", "missing"]
    source: Literal["seed_test", "manual"] = "seed_test"
    campaign_id: Optional[str] = None
    email_id: Optional[str] = None
    observed_at: Optional[datetime] = None
    notes: Optional[str] = Field(None, max_length=500)


class LeadUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    company: Optional[str] = Field(None, max_length=200)
    persona: Optional[Literal["contractor", "architect", "homeowner", "other"]] = None
    zip: Optional[str] = Field(None, max_length=10)
    state: Optional[str] = Field(None, max_length=40)
    timezone: Optional[str] = Field(None, max_length=60)
    lifecycle_stage: Optional[Literal["new", "mql", "sql", "customer", "lost"]] = None  # the outcome lead scoring learns from


class LeadEmailCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=20_000)
    reply_id: Optional[str] = None  # answering this reply: the email joins the same conversation in their inbox
