"""One email to one lead, written by hand: a reply to what they wrote, or a follow-up whenever you choose.

It goes out like a campaign email (same From, Reply-To and layout) and is logged, so it shows in Sent emails and its
opens and clicks are tracked. Answering a reply sets In-Reply-To / References, so it lands in the same conversation in
their inbox instead of starting a new one.
"""
import re
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.core.config import settings
from app.db.supabase import get_supabase
from app.services import email_layout
from app.services.campaign_service import campaign_service, reply_address, unsubscribe_header

FIRST_NAME_RE = re.compile(r"{{\s*first_name\s*}}", re.I)


def build_message(lead: dict, subject: str, body: str, thread: str | None = None) -> dict:
    """The Resend message: {{first_name}} filled in, and threading headers when answering a reply."""
    first_name = (lead.get("name") or "").split(" ")[0] or "there"
    body = FIRST_NAME_RE.sub(first_name, body)
    sender = {"from_email": settings.DEFAULT_FROM_EMAIL, "reply_to": None}
    headers = {"List-Unsubscribe": unsubscribe_header(sender)}
    if thread:
        headers.update({"In-Reply-To": thread, "References": thread})
    message = {
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": [lead["email"]],
        "subject": subject,
        "html": email_layout.campaign_email(body, "text", brand=email_layout.brand_name(settings.DEFAULT_FROM_EMAIL)),
        "text": body,
        "headers": headers,
    }
    if reply_address(sender):
        message["reply_to"] = reply_address(sender)
    return message


def send(user_id: str, lead_id: str, subject: str, body: str, reply_id: str | None = None) -> dict:
    db = get_supabase()
    leads = db.table("leads").select("email, name").eq("id", lead_id).eq("user_id", user_id).limit(1).execute().data
    if not leads:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found.")
    lead = leads[0]
    thread = None
    if reply_id:
        rows = db.table("email_replies").select("message_id").eq("id", reply_id).eq("user_id", user_id).limit(1).execute().data
        thread = rows[0]["message_id"] if rows else None

    message = build_message(lead, subject, body, thread)
    body = message["text"]
    (state, email_id, error), = campaign_service._send_chunk([message])
    log = {
        "id": email_id or f"failed_{datetime.now(timezone.utc).timestamp()}",
        "user_id": user_id, "campaign_id": None, "variant": None, "recipient": lead["email"],
        "from_email": settings.DEFAULT_FROM_EMAIL, "subject": subject, "status": state, "error": error,
        "snippet": body[:200], "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    db.table("email_logs").insert(log).execute()
    if state == "failed":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The email couldn't be sent: {error}")
    return {"email_id": log["id"], "status": state, "to": lead["email"]}
