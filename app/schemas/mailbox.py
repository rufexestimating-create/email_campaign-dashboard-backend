from typing import Annotated, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class MailboxCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=80)
    # Empty = Hostinger's servers
    smtp_host: Optional[str] = Field(None, max_length=255)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    imap_host: Optional[str] = Field(None, max_length=255)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = Field(None, max_length=255)
    daily_limit: Optional[int] = Field(None, ge=2, le=50)


class WarmupAdvanced(BaseModel):
    slow_ramp: Optional[bool] = None
    random_daily: Optional[bool] = None
    open_rate: Optional[int] = Field(None, ge=0, le=100)
    spam_protection: Optional[int] = Field(None, ge=0, le=100)
    important_rate: Optional[int] = Field(None, ge=0, le=100)
    read_emulation: Optional[bool] = None
    timezone: Optional[str] = Field(None, max_length=64)
    window_start: Optional[int] = Field(None, ge=0, le=23)
    window_end: Optional[int] = Field(None, ge=1, le=24)
    topic: Optional[Literal["architecture", "general", "construction", "real_estate", "marketing", "software", "finance", "custom"]] = None
    custom_subjects: Optional[list[Annotated[str, Field(max_length=150)]]] = Field(None, max_length=50)
    custom_messages: Optional[list[Annotated[str, Field(max_length=2000)]]] = Field(None, max_length=30)
    min_gap_minutes: Optional[int] = Field(None, ge=0, le=240)
    reply_delay: Optional[Literal["fast", "normal", "slow"]] = None


class ContentPreview(BaseModel):
    mailbox_id: Optional[str] = None
    topic: Optional[str] = Field(None, max_length=40)
    custom_subjects: list[Annotated[str, Field(max_length=150)]] = Field(default_factory=list, max_length=50)
    custom_messages: list[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list, max_length=30)


class MailboxUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)
    daily_limit: Optional[int] = Field(None, ge=2, le=50)
    warmup_increase: Optional[int] = Field(None, ge=1, le=10)      # emails more each day (Instantly's "Increase per day")
    warmup_reply_rate: Optional[int] = Field(None, ge=0, le=100)   # % of received warm-up emails answered
    warmup_settings: Optional[WarmupAdvanced] = None             # Instantly's advanced settings
    warmup_weekdays_only: Optional[bool] = None                    # no warm-up emails on Saturday and Sunday
    status: Optional[Literal["active", "paused"]] = None
    password: Optional[str] = Field(None, min_length=1, max_length=200)


class NewsletterAdd(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sender_domains: str = Field(min_length=3, max_length=300)
    signup_url: Optional[str] = Field(None, max_length=500)
