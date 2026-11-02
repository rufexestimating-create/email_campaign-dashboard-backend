import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.api.deps import get_current_user, spam_detector_on
from app.db.supabase import get_supabase
from app.schemas.insights import SeedCreate, SendPlanRequest, WarmupUpdate
from app.services import placement_model, send_time, warmup
from app.services.local_time import is_valid_timezone

router = APIRouter(dependencies=[Depends(get_current_user)])


def _default_tz(tz: str) -> str:
    if not is_valid_timezone(tz):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown timezone: {tz}")
    return tz


@router.get("/send-time", summary="Local-time heatmaps (persona x device x weekday x hour), send windows and reply delays")
def send_time_insights(
    days: int = Query(180, ge=7, le=730),
    timezone: str = Query(send_time.DEFAULT_TIMEZONE, description="Assumed for contacts with no ZIP, state or timezone"),
    user: dict = Depends(get_current_user),
):
    return send_time.analyze(user["id"], days, _default_tz(timezone))


@router.post("/send-plan", summary="Best send time and follow-up delay for each recipient")
def send_plan(data: SendPlanRequest, user: dict = Depends(get_current_user)):
    return send_time.plan(user["id"], data.recipients, data.days, _default_tz(data.timezone))


@router.get("/warmup", summary="Domain warm-up: today's sending limit, the weekly schedule and the seed inboxes")
def get_warmup(user: dict = Depends(get_current_user)):
    return {**warmup.today_status(user["id"]), "seeds": warmup.list_seeds(user["id"]), "seed_limit": warmup.SEED_LIMIT}


@router.put("/warmup", summary="Turn the warm-up on or off, change its start date, or stop adding seed inboxes")
def update_warmup(data: WarmupUpdate, user: dict = Depends(get_current_user)):
    warmup.update_settings(user["id"], data.model_dump())
    return get_warmup(user)


@router.post("/warmup/seeds", status_code=status.HTTP_201_CREATED, summary="Add one of your own test inboxes")
def add_seed(data: SeedCreate, user: dict = Depends(get_current_user)):
    try:
        return warmup.add_seed(user["id"], data.email)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.delete("/warmup/seeds/{seed_id}", summary="Remove a test inbox")
def remove_seed(seed_id: str, user: dict = Depends(get_current_user)):
    if not warmup.remove_seed(user["id"], seed_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test inbox not found.")
    return {"message": "Removed."}


@router.get("/model", dependencies=[Depends(spam_detector_on)], summary="The placement model in use: version, training data, metrics and strongest signals")
def model_info():
    path = placement_model.latest_model_path()
    if path is None:
        return {"available": False, "weight": None}
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    kind = placement_model._label_kind(meta)
    return {"available": True, "label_kind": kind, "weight": placement_model.MODEL_WEIGHT[kind], **meta}


@router.get("/model/calibration.png", dependencies=[Depends(spam_detector_on)], summary="Calibration plot of the model in use")
def model_calibration():
    path = placement_model.latest_model_path()
    plot = path and path.parent / json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["files"]["calibration_plot"]
    if not plot or not plot.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No calibration plot yet. Train a model first.")
    return FileResponse(plot, media_type="image/png")


@router.get("/tracking-summary", summary="How much tracking data exists: leads, events by type, placement labels")
def tracking_summary(user: dict = Depends(get_current_user)):
    db = get_supabase()
    count = lambda table, **eq: _count(db, table, user["id"], **eq)
    events = {t: count("email_events", event_type=t) for t in ("delivered", "opened", "clicked", "replied", "bounced", "complained")}
    labels = Counter(r["placement"] for r in db.table("placement_labels").select("placement").eq("user_id", user["id"]).limit(10000).execute().data)
    return {
        "leads": count("leads"),
        "leads_with_persona": _count(db, "leads", user["id"], not_null="persona"),
        "leads_with_timezone": _count(db, "leads", user["id"], not_null="timezone"),
        "events": events,
        "placement_labels": dict(labels),
        "usable_labels": labels["spam"] + labels["complained"] + labels["inbox"] + labels["promotions"],
    }


def _count(db, table: str, user_id: str, not_null: str | None = None, **eq) -> int:
    q = db.table(table).select("id", count="exact").eq("user_id", user_id).limit(1)
    for k, v in eq.items():
        q = q.eq(k, v)
    if not_null:
        q = q.not_.is_(not_null, "null")
    return q.execute().count or 0
