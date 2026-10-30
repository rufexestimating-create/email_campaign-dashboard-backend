import uuid
from datetime import datetime, timezone
from typing import List
import resend
from postgrest import CountMethod

from app.core.config import settings
from app.db.supabase import get_supabase
from app.schemas.email import SendEmailRequest, SendEmailResponse, EmailLogEntry


class EmailService:
    """Sends email through Resend and stores each dispatch in the Supabase `email_logs` table."""

    def is_configured(self) -> bool:
        return bool(settings.RESEND_API_KEY and settings.RESEND_API_KEY.strip() and not settings.RESEND_API_KEY.startswith("re_your_"))

    def _save_log(self, user_id: str, entry: EmailLogEntry):
        get_supabase().table("email_logs").insert({
            "id": entry.id,
            "user_id": user_id,
            "recipient": entry.recipient,
            "from_email": entry.from_email,
            "subject": entry.subject,
            "status": entry.status,
            "error": entry.error,
            "snippet": entry.snippet,
            "sent_at": entry.sent_at.isoformat(),
        }).execute()

    async def send_email(self, request: SendEmailRequest, user_id: str) -> SendEmailResponse:
        from_address = request.from_email or settings.DEFAULT_FROM_EMAIL
        recipients = [request.to] if isinstance(request.to, str) else request.to
        recipient_str = ", ".join(recipients)

        # Prepare content
        html_content = request.html
        if not html_content and request.text:
            html_content = f"<p>{request.text.replace(chr(10), '<br>')}</p>"
        elif not html_content:
            html_content = "<p>No content provided</p>"
        snippet = (request.text or html_content)[:200]

        # If API key is not configured, inform the user with actionable feedback
        if not self.is_configured():
            simulated_id = f"sim_{uuid.uuid4().hex[:10]}"
            self._save_log(user_id, EmailLogEntry(
                id=simulated_id,
                recipient=recipient_str,
                from_email=from_address,
                subject=request.subject,
                status="simulated",
                sent_at=datetime.now(timezone.utc),
                error="RESEND_API_KEY is not configured in backend/.env. Simulated send for preview.",
                snippet=snippet,
            ))
            return SendEmailResponse(
                success=True,
                email_id=simulated_id,
                message="Simulated successfully. Add your RESEND_API_KEY to backend/.env to send live emails.",
                recipient=request.to,
                subject=request.subject,
                sent_at=datetime.now(timezone.utc),
            )

        # Set API key for Resend SDK
        resend.api_key = settings.RESEND_API_KEY

        params: resend.Emails.SendParams = {
            "from": from_address,
            "to": recipients,
            "subject": request.subject,
            "html": html_content,
        }
        if request.reply_to:
            params["reply_to"] = request.reply_to
        if request.text:
            params["text"] = request.text

        try:
            email_resp = resend.Emails.send(params)
            resend_id = email_resp.get("id") if isinstance(email_resp, dict) else getattr(email_resp, "id", str(uuid.uuid4()))
        except Exception as e:
            error_msg = str(e)
            self._save_log(user_id, EmailLogEntry(
                id=str(uuid.uuid4()),
                recipient=recipient_str,
                from_email=from_address,
                subject=request.subject,
                status="failed",
                sent_at=datetime.now(timezone.utc),
                error=error_msg,
                snippet=snippet,
            ))
            raise RuntimeError(f"Resend API Error: {error_msg}")

        self._save_log(user_id, EmailLogEntry(
            id=resend_id,
            recipient=recipient_str,
            from_email=from_address,
            subject=request.subject,
            status="sent",
            sent_at=datetime.now(timezone.utc),
            snippet=snippet,
        ))
        return SendEmailResponse(
            success=True,
            email_id=resend_id,
            message="Email dispatched successfully via Resend API",
            recipient=request.to,
            subject=request.subject,
            sent_at=datetime.now(timezone.utc),
        )

    def get_logs(self, user_id: str, limit: int = 50) -> List[EmailLogEntry]:
        res = (
            get_supabase().table("email_logs")
            .select("id, recipient, from_email, subject, status, error, snippet, sent_at, campaign_id, variant, last_event")
            .eq("user_id", user_id)
            .order("sent_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [EmailLogEntry(**row) for row in res.data]

    def _count(self, user_id: str, status: str | None = None) -> int:
        query = get_supabase().table("email_logs").select("id", count=CountMethod.exact, head=True).eq("user_id", user_id)
        if status:
            query = query.eq("status", status)
        return query.execute().count or 0

    def get_stats(self, user_id: str) -> dict:
        total = self._count(user_id)
        sent = self._count(user_id, "sent")
        simulated = self._count(user_id, "simulated")
        failed = self._count(user_id, "failed")
        success_rate = round(((sent + simulated) / total * 100), 1) if total > 0 else 100.0

        return {
            "total_emails": total,
            "sent_count": sent,
            "simulated_count": simulated,
            "failed_count": failed,
            "success_rate": success_rate,
            "is_live_ready": self.is_configured(),
            "default_from": settings.DEFAULT_FROM_EMAIL
        }


email_service = EmailService()
