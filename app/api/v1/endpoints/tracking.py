import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.supabase import get_supabase
from app.schemas.tracking import LeadUpdate, PlacementLabelCreate, ReplyCreate
from app.services import event_tracking, follow_ups, lead_scoring, reply_inbox
from app.services.local_time import is_valid_timezone

logger = logging.getLogger(__name__)

# Called by Resend, not by the browser: authenticated by the webhook signature instead of a login
webhook_router = APIRouter()
router = APIRouter(dependencies=[Depends(get_current_user)])


@webhook_router.post("/resend", summary="Resend webhook: delivered / opened / clicked / bounced / complained events")
async def resend_webhook(request: Request):
    if not settings.RESEND_WEBHOOK_SECRET:
        # Refuse rather than accept unsigned events; Resend keeps retrying until this is configured
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RESEND_WEBHOOK_SECRET is not set on the server.")
    body = await request.body()
    try:
        event_tracking.verify_signature(settings.RESEND_WEBHOOK_SECRET, request.headers, body)
        payload = json.loads(body)
    except (event_tracking.WebhookSignatureError, ValueError) as e:
        logger.warning("Rejected Resend webhook: %s", e)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature.")
    result = await run_in_threadpool(event_tracking.handle_resend_event, payload, request.headers.get("svix-id"))
    return {"result": result}


@router.post("/events/reply", status_code=status.HTTP_201_CREATED, summary="Record that a contact replied")
def record_reply(data: ReplyCreate, user: dict = Depends(get_current_user)):
    row = event_tracking.record_reply(user["id"], data.recipient, data.replied_at, data.campaign_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No email was sent to that address.")
    return row


@router.post("/placement-labels", status_code=status.HTTP_201_CREATED, summary="Record where an email landed (seed test)")
def add_placement_label(data: PlacementLabelCreate, user: dict = Depends(get_current_user)):
    row = event_tracking.add_placement_label(user["id"], data.model_dump())
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That email or campaign wasn't sent to this address.")
    return row


@router.get("/placement-labels", summary="Placement labels (seed tests, complaints, bounces)")
def list_placement_labels(limit: int = Query(200, ge=1, le=1000), user: dict = Depends(get_current_user)):
    return get_supabase().table("placement_labels").select("*").eq("user_id", user["id"]).order("observed_at", desc=True).limit(limit).execute().data


@router.get("/leads", summary="Contacts with persona, ZIP, state and timezone")
def list_leads(limit: int = Query(500, ge=1, le=1000), user: dict = Depends(get_current_user)):
    return get_supabase().table("leads").select("*").eq("user_id", user["id"]).order("updated_at", desc=True).limit(limit).execute().data


@router.get("/leads/scores", summary="Lead scores: P(reply), P(MQL), P(SQL), band, segment, and sales alerts (P(SQL) > 0.6)")
def lead_scores(user: dict = Depends(get_current_user)):
    return lead_scoring.scores_overview(user["id"])


@router.post("/leads/score", summary="Score every lead now (everyone you've emailed becomes a lead)")
def score_leads(user: dict = Depends(get_current_user)):
    return lead_scoring.score_user(user["id"])


@router.patch("/leads/{lead_id}", summary="Update a contact's persona, ZIP, state, timezone or lifecycle stage (new / mql / sql / customer / lost)")
def update_lead(lead_id: str, data: LeadUpdate, user: dict = Depends(get_current_user)):
    changes = data.model_dump(exclude_unset=True)
    if changes.get("timezone") and not is_valid_timezone(changes["timezone"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown timezone. Use a name like America/Chicago.")
    row = event_tracking.update_lead(user["id"], lead_id, changes)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found.")
    return row


@router.get("/replies", summary="Newest replies read from the reply mailbox: who replied, to which campaign, and what they wrote")
def replies(limit: int = Query(50, ge=1, le=200), user: dict = Depends(get_current_user)):
    return {"tracking_on": reply_inbox.is_configured(), "reply_to": settings.REPLY_TO_EMAIL or None,
            "replies": reply_inbox.recent(user["id"], limit)}


def _lead_email(user_id: str, lead_id: str) -> str:
    rows = get_supabase().table("leads").select("email").eq("id", lead_id).eq("user_id", user_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found.")
    return rows[0]["email"]


@router.get("/leads/{lead_id}/follow-ups", summary="How many of this contact's follow-ups are still waiting, and when the next is due")
def lead_follow_ups(lead_id: str, user: dict = Depends(get_current_user)):
    return follow_ups.waiting_for(user["id"], _lead_email(user["id"], lead_id))


@router.post("/leads/{lead_id}/follow-ups/send-now", summary="Send this contact's waiting follow-ups now instead of at their scheduled time")
def lead_follow_ups_send_now(lead_id: str, user: dict = Depends(get_current_user)):
    return {"queued": follow_ups.send_now_for(user["id"], _lead_email(user["id"], lead_id))}
