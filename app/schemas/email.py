from typing import List, Optional, Union
from pydantic import BaseModel, EmailStr, Field
from datetime import datetime


class SendEmailRequest(BaseModel):
    to: Union[EmailStr, List[EmailStr]]
    subject: str = Field(..., min_length=1, max_length=255)
    html: Optional[str] = None
    text: Optional[str] = None
    from_email: Optional[str] = None
    reply_to: Optional[str] = None


class SendEmailResponse(BaseModel):
    success: bool
    email_id: Optional[str] = None
    message: str
    recipient: Union[str, List[str]]
    subject: str
    sent_at: datetime


class EmailLogEntry(BaseModel):
    id: str
    recipient: str
    from_email: str
    subject: str
    status: str  # "sent", "failed", "simulated"
    sent_at: datetime
    error: Optional[str] = None
    snippet: Optional[str] = None
    campaign_id: Optional[str] = None
    variant: Optional[str] = None
    last_event: Optional[str] = None


class ResendConfigStatus(BaseModel):
    api_key_configured: bool
    default_from_email: str
    resend_domain_status: str
