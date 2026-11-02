from typing import Literal, Optional

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


class MailboxUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)
    daily_limit: Optional[int] = Field(None, ge=2, le=50)
    status: Optional[Literal["active", "paused"]] = None
    password: Optional[str] = Field(None, min_length=1, max_length=200)
