import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.schemas.campaign import CampaignCreate, CronPreviewRequest, EmailPreviewRequest, PrecheckRequest, PreflightDecisionCreate, PreflightRequest
from app.services import campaign_advisor, email_layout, follow_ups, preflight
from app.services.campaign_service import campaign_service, personal_values, render, upcoming_cron_runs

router = APIRouter(dependencies=[Depends(get_current_user)])
# Also served at /api/v1/campaign/preflight, the path named in the milestone plan
campaign_router = APIRouter(dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def _enforce_preflight(user_id: str, data: CampaignCreate):
    """The pre-send review, enforced on the server: a failed check blocks the send unless an override reason is given.
    If the checks themselves can't run (e.g. the database is briefly unreachable) the send isn't blocked."""
    try:
        result = preflight.preflight(user_id, {
            "subject": data.subject, "body": data.body, "content_type": data.content_type, "ab_test": data.ab_test.model_dump(),
            "recipients": [r.email for r in data.recipients], "schedule": data.schedule.model_dump(),
            "reply_to": data.reply_to, "from_email": data.from_email,
        })
    except Exception as e:
        logger.warning("Preflight could not run before sending; not blocking: %s", e)
        return
    if result["verdict"] == "fail" and len((data.override_reason or "").strip()) < preflight.MIN_OVERRIDE_REASON:
        failed = ", ".join(c["name"] for c in result["checks"] if c["status"] == "fail")
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {
            "code": "preflight_failed",
            "message": f"The pre-send review failed ({failed}). Fix it, or give a reason to send anyway.",
        })


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a campaign (send now, schedule, recurring or draft)")
def create_campaign(data: CampaignCreate, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    if not data.save_as_draft:
        _enforce_preflight(user["id"], data)
    campaign = campaign_service.create(user["id"], data)
    if campaign["status"] == "sending":
        background_tasks.add_task(campaign_service.start_run, campaign["id"])
    return campaign


@router.get("", summary="List campaigns with A/B stats")
def list_campaigns(user: dict = Depends(get_current_user)):
    return campaign_service.list_for_user(user["id"])


@router.post("/cron-preview", summary="Validate a cron expression and show the next run times")
def cron_preview(data: CronPreviewRequest):
    return {"next_runs": upcoming_cron_runs(data.cron.strip(), data.timezone, 3)}


@router.post("/precheck", summary="Pre-send recommendations: spam risk, past behaviour of these contacts, timing, limits")
def precheck(data: PrecheckRequest, user: dict = Depends(get_current_user)):
    return campaign_advisor.precheck(user["id"], data.model_dump())


@router.post("/preflight", summary="Eight pass / warn / fail checks before sending, each with a message and a one-click action")
@campaign_router.post("/preflight", summary="Eight pass / warn / fail checks before sending, each with a message and a one-click action")
def run_preflight(data: PreflightRequest, user: dict = Depends(get_current_user)):
    return preflight.preflight(user["id"], data.model_dump())


@campaign_router.post("/preflight/decisions", status_code=status.HTTP_201_CREATED, summary="Log what was done in the pre-send pop-up (accepted / ignored advice, override reason)")
def log_preflight_decision(data: PreflightDecisionCreate, user: dict = Depends(get_current_user)):
    try:
        return preflight.record_decision(user["id"], data.model_dump())
    except preflight.OverrideReasonRequired as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@campaign_router.get("/preflight/decisions", summary="Pre-send decisions, with how often each check's advice was taken or ignored")
def list_preflight_decisions(limit: int = Query(100, ge=1, le=500), user: dict = Depends(get_current_user)):
    return preflight.list_decisions(user["id"], limit)


@router.post("/email-preview", summary="Render an email body in the branded layout, exactly as recipients will see it")
def email_preview(data: EmailPreviewRequest):
    values = personal_values(data.recipient.model_dump(), greeting_fallback=True) if data.recipient else {}
    is_html = data.content_type == "html"
    body = render(data.body, values, html=is_html)
    brand = (data.sender_name or "").strip() or email_layout.brand_name()
    return {"html": email_layout.campaign_email(body, data.content_type, brand=brand)}


@router.get("/{campaign_id}", summary="Campaign details, variant stats and recent sends")
def get_campaign(campaign_id: str, user: dict = Depends(get_current_user)):
    return campaign_service.detail(user["id"], campaign_id)


@router.post("/{campaign_id}/send-now", summary="Send the campaign immediately")
def send_now(campaign_id: str, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    campaign_service.send_now(user["id"], campaign_id)
    background_tasks.add_task(campaign_service.start_run, campaign_id)
    return {"message": "Sending started."}


@router.post("/{campaign_id}/pause", summary="Pause a scheduled campaign, or stop a sending one partway")
def pause(campaign_id: str, user: dict = Depends(get_current_user)):
    campaign_service.pause(user["id"], campaign_id)
    return {"message": "Campaign paused."}


@router.post("/{campaign_id}/resume", summary="Carry on a paused send, or activate the campaign's schedule")
def resume(campaign_id: str, user: dict = Depends(get_current_user)):
    state = campaign_service.resume(user["id"], campaign_id)
    return {"message": "Sending resumed." if state == "sending" else "Campaign scheduled.", "status": state}


@router.post("/{campaign_id}/refresh-events", summary="Sync delivered/opened/clicked status from Resend")
def refresh_events(campaign_id: str, user: dict = Depends(get_current_user)):
    updated = campaign_service.refresh_events(user["id"], campaign_id)
    return {"updated": updated}


@router.get("/{campaign_id}/follow-ups", summary="Follow-up steps, who got each one, and why some were skipped")
def get_follow_ups(campaign_id: str, user: dict = Depends(get_current_user)):
    campaign_service._get_owned(user["id"], campaign_id)
    return follow_ups.detail(user["id"], campaign_id)


@router.post("/{campaign_id}/follow-ups/stop", summary="Cancel every follow-up of this campaign that hasn't gone out yet")
def stop_follow_ups(campaign_id: str, user: dict = Depends(get_current_user)):
    campaign_service._get_owned(user["id"], campaign_id)
    return {"cancelled": follow_ups.cancel(user["id"], campaign_id)}


@router.delete("/{campaign_id}", summary="Delete a campaign")
def delete_campaign(campaign_id: str, user: dict = Depends(get_current_user)):
    campaign_service.delete(user["id"], campaign_id)
    return {"message": "Campaign deleted."}
