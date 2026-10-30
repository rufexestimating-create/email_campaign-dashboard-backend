from fastapi import APIRouter, Depends, HTTPException, Query
from app.api.deps import get_current_user
from app.schemas.email import (
    SendEmailRequest,
    SendEmailResponse,
    EmailLogEntry,
    ResendConfigStatus,
)
from app.services.email_service import email_service
from app.core.config import settings

# Every email route requires a valid JWT
router = APIRouter(dependencies=[Depends(get_current_user)])


@router.post("/send", response_model=SendEmailResponse, summary="Send an email via Resend")
async def send_email(request: SendEmailRequest, user: dict = Depends(get_current_user)):
    try:
        response = await email_service.send_email(request, user_id=user["id"])
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/logs", response_model=list[EmailLogEntry], summary="Get email delivery logs")
def get_logs(limit: int = Query(50, ge=1, le=100), user: dict = Depends(get_current_user)):
    return email_service.get_logs(user_id=user["id"], limit=limit)


@router.get("/stats", summary="Get email delivery statistics")
def get_stats(user: dict = Depends(get_current_user)):
    return email_service.get_stats(user_id=user["id"])


@router.get("/config-status", summary="Check Resend and Hostinger config status")
def get_config_status():
    is_live = email_service.is_configured()
    return {
        "api_key_configured": is_live,
        "default_from_email": settings.DEFAULT_FROM_EMAIL,
        "mode": "live" if is_live else "preview/simulation",
        "hostinger_guide": {
            "title": "How to link your Hostinger Domain with Resend",
            "steps": [
                "1. Go to Resend (https://resend.com/domains) and click 'Add Domain'.",
                "2. Enter your domain hosted on Hostinger (e.g. yourdomain.com).",
                "3. Resend will display DNS records: SPF (TXT), DKIM (TXT), and MX or DMARC records.",
                "4. Log in to Hostinger hPanel -> Domains -> DNS / Nameservers -> DNS Records.",
                "5. Add the TXT records provided by Resend (Host/Name and Value).",
                "6. Click 'Verify Records' in Resend. Once verified, configure DEFAULT_FROM_EMAIL in backend/.env to your custom address (e.g., hello@yourdomain.com)."
            ]
        }
    }
