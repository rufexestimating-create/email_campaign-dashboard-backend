from typing import Dict, List, Literal, Optional
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field

MAX_RECIPIENTS = 5000


MAX_OWN_BODY = 20_000


class RecipientIn(BaseModel):
    email: str = Field(..., max_length=320)
    name: Optional[str] = Field(None, max_length=200)
    # Extra CSV columns, usable in subject/body as {{column_name}}
    variables: Dict[str, str] = {}
    # This person's own email, written for them in the composer; replaces the campaign's subject / message for them
    subject: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = Field(None, max_length=MAX_OWN_BODY)


class ABTestIn(BaseModel):
    enabled: bool = False
    subject_b: Optional[str] = Field(None, max_length=255)
    body_b: Optional[str] = Field(None, max_length=100_000)
    split: int = Field(50, ge=1, le=99, description="Percent of recipients that receive variant A")


class ScheduleIn(BaseModel):
    type: Literal["now", "once", "recurring"]
    send_at: Optional[datetime] = None
    cron: Optional[str] = Field(None, max_length=100)
    timezone: str = "UTC"


class FollowUpIn(BaseModel):
    # At least this long after the previous email (longer for slow repliers). 0 = a quick test: about 10 minutes later.
    wait_days: int = Field(..., ge=0, le=30)
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=100_000)


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    from_email: Optional[str] = Field(None, max_length=320)
    reply_to: Optional[EmailStr] = None
    content_type: Literal["text", "html"] = "text"
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=100_000)
    ab_test: ABTestIn = ABTestIn()
    recipients: List[RecipientIn] = Field(..., min_length=1, max_length=MAX_RECIPIENTS)
    schedule: ScheduleIn
    save_as_draft: bool = False
    follow_ups: List[FollowUpIn] = Field(default_factory=list, max_length=2)
    # How fast the emails go out: safe = one every 15-30 s, normal = every 5-10 s, fast = all at once (small lists)
    send_speed: Literal["safe", "normal", "fast"] = "safe"
    # Required when the pre-send review has a failed check: the server re-runs it and refuses the send without one
    override_reason: Optional[str] = Field(None, max_length=500)


class SpamCheckRequest(BaseModel):
    subject: str = Field("", max_length=255)
    body: str = Field("", max_length=100_000)
    content_type: Literal["text", "html"] = "text"


class SpamCheckBatchRequest(BaseModel):
    items: List[SpamCheckRequest] = Field(..., max_length=50)


class SpamKeywordCreate(BaseModel):
    phrase: str = Field(..., min_length=2, max_length=80)
    severity: Literal["high", "medium", "low"] = "medium"
    alternative: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=240)


class PrecheckRequest(BaseModel):
    subject: str = Field("", max_length=255)
    body: str = Field("", max_length=100_000)
    content_type: Literal["text", "html"] = "text"
    ab_test: ABTestIn = ABTestIn()
    recipients: List[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    schedule: ScheduleIn
    reply_to: Optional[str] = None


class PreflightRequest(PrecheckRequest):
    from_email: Optional[str] = Field(None, max_length=320)  # sending domain for the SPF / DKIM / DMARC check


class PreflightCheckSummary(BaseModel):
    id: str = Field(..., max_length=40)
    status: Literal["pass", "warn", "fail"]
    message: str = Field("", max_length=500)


class PreflightDecisionCreate(BaseModel):
    campaign_id: Optional[str] = None
    verdict: Literal["pass", "warn", "fail"]
    checks: List[PreflightCheckSummary] = Field(..., max_length=20)  # the latest result shown in the pop-up
    accepted: List[str] = Field(default_factory=list, max_length=40)  # check ids whose one-click action was applied
    override_reason: Optional[str] = Field(None, max_length=500)
    outcome: Literal["sent", "scheduled", "cancelled"]


class EmailPreviewRequest(BaseModel):
    body: str = Field("", max_length=100_000)
    content_type: Literal["text", "html"] = "text"
    sender_name: Optional[str] = Field(None, max_length=80)
    recipient: Optional[RecipientIn] = None


class CronPreviewRequest(BaseModel):
    cron: str = Field(..., max_length=100)
    timezone: str = "UTC"


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=100_000)
    content_type: Literal["text", "html"] = "text"
