"""Automated follow-ups (M10).

A one-off campaign can carry up to two follow-up emails. After the campaign goes out, step 1 is queued per recipient;
when a step is sent, the next one is queued from that email. Each step:
  - waits `wait_days`, or longer when this contact (or your contacts overall) usually takes a while to reply
    (send_time.follow_up_after_hours, the reply-delay rule)
  - then goes out at the start of the contact's next best send window, in their own timezone (send_time.window_for)
  - is skipped, and later steps with it, if by then the contact replied, bounced, marked an email as spam, was moved
    to SQL / customer / lost, or became sales-ready (lead scoring): a person should follow up then, not a sequence.
The scheduler loop (scheduler.py) sends due steps every 30 seconds.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.services import send_time

logger = logging.getLogger(__name__)

MAX_STEPS = 2
CLAIM_LIMIT = 100
# wait_days = 0: a test follow-up, sent this many minutes after the previous email (no best-time window, no stretching)
TEST_WAIT_MINUTES = 10
STOP_STAGES = {"sql": "Moved to sales-qualified", "customer": "Became a customer", "lost": "Marked as lost"}
STOP_REASONS = {
    "replied": "Replied",
    "bounced": "Email bounced",
    "complained": "Marked an email as spam",
    "sales_ready": "Sales-ready: handed to sales",
    "earlier_step": "An earlier follow-up was stopped",
    "cancelled": "Stopped by you",
}


# ---------- Decisions (pure) ----------

def stop_reason(state: dict) -> str | None:
    """Why a follow-up must not go out, or None. `state`: replied, bounced, complained (bools), stage, band."""
    if state.get("replied"):
        return STOP_REASONS["replied"]
    if state.get("bounced"):
        return STOP_REASONS["bounced"]
    if state.get("complained"):
        return STOP_REASONS["complained"]
    if state.get("stage") in STOP_STAGES:
        return STOP_STAGES[state["stage"]]
    if state.get("band") == "sales":
        return STOP_REASONS["sales_ready"]
    return None


def wait_hours(wait_days: int, slow_reply_hours: int | None) -> int:
    """The chosen wait, stretched to the reply-delay rule when this contact replies slowly."""
    return max(wait_days * 24, slow_reply_hours or 0)


def due_time(anchor_utc: datetime, wait_days: int, recipient: str, lead: dict | None, ctx: dict, wait_minutes: int | None = None) -> datetime:
    """When a follow-up should go out: after the wait, at the start of the contact's next best window (UTC).
    A wait in minutes (or wait_days 0) is sent right after it, with no window and no slow-reply stretch."""
    if wait_minutes:
        return anchor_utc + timedelta(minutes=wait_minutes)
    if wait_days == 0:
        return anchor_utc + timedelta(minutes=TEST_WAIT_MINUTES)
    delays = ctx["delays"]
    lead_delay = delays["per_lead"].get(recipient)
    slow = lead_delay["follow_up_after_hours"] if lead_delay else delays["follow_up_after_hours"]
    earliest = anchor_utc + timedelta(hours=wait_hours(wait_days, slow))
    tz = (lead or {}).get("timezone") if send_time.is_valid_timezone((lead or {}).get("timezone")) else ctx["default_tz"]
    window = send_time.window_for(recipient, lead, ctx["profiles"], ctx["segments"])["window"]
    if send_time.in_window(send_time.to_local(earliest, tz), window):
        return earliest  # the wait already ends inside their best window: send then, not at the next window
    return send_time.next_window_start(earliest, tz, window).astimezone(timezone.utc)


# ---------- Database helpers ----------

def _db():
    from app.db.supabase import get_supabase

    return get_supabase()


def _utc(value) -> datetime:
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def timing_context(user_id: str, default_tz: str) -> dict:
    """Leads, learned send windows and reply delays for one user (send_time.py), loaded once per batch."""
    leads = send_time.load_leads(user_id)
    _, rows, delays = send_time._learn(user_id, 180, default_tz, leads)
    return {"leads": leads, "profiles": send_time.lead_profiles(rows), "segments": send_time.segment_windows(rows),
            "delays": delays, "default_tz": default_tz}


def _default_tz(campaign: dict) -> str:
    """Timezone assumed for contacts with no ZIP / state: US Eastern, not the sender's (who may be abroad)."""
    return send_time.DEFAULT_TIMEZONE


def save_steps(user_id: str, campaign_id: str, steps: list[dict]):
    rows = [{"user_id": user_id, "campaign_id": campaign_id, "step": i + 1, "wait_days": s["wait_days"],
             "subject": s["subject"], "body": s["body"],
             # Only sent when used, so days-only follow-ups keep working on a database without the column yet
             **({"wait_minutes": s["wait_minutes"]} if s.get("wait_minutes") else {})}
            for i, s in enumerate(steps[:MAX_STEPS])]
    if rows:
        _db().table("campaign_follow_ups").insert(rows).execute()


def _step(campaign_id: str, step: int) -> dict | None:
    rows = _db().table("campaign_follow_ups").select("*").eq("campaign_id", campaign_id).eq("step", step).limit(1).execute().data
    return rows[0] if rows else None


def queue_step(campaign: dict, step: int, anchors: list[dict], ctx: dict | None = None) -> int:
    """Queues `step` for each anchor {recipient, email_id, sent_at}. Returns how many were queued."""
    follow_up = _step(campaign["id"], step)
    if not follow_up or not anchors:
        return 0
    ctx = ctx or timing_context(campaign["user_id"], _default_tz(campaign))
    rows = [{
        "user_id": campaign["user_id"], "campaign_id": campaign["id"], "follow_up_id": follow_up["id"], "step": step,
        "recipient": a["recipient"], "anchor_email_id": a["email_id"],
        "due_at": due_time(_utc(a["sent_at"]), follow_up["wait_days"], a["recipient"], ctx["leads"].get(a["recipient"]), ctx,
                           follow_up.get("wait_minutes")).isoformat(),
    } for a in anchors]
    for i in range(0, len(rows), 500):
        _db().table("follow_up_sends").upsert(rows[i:i + 500], on_conflict="follow_up_id,recipient", ignore_duplicates=True).execute()
    return len(rows)


def after_campaign_sent(campaign: dict):
    """Called once a one-off campaign has gone out: queue step 1 for everyone it reached."""
    if campaign.get("schedule_type") == "recurring":
        return
    logs = _db().table("email_logs").select("id, recipient, sent_at, status, variant").eq("campaign_id", campaign["id"]) \
        .in_("status", ["sent", "simulated"]).in_("variant", ["A", "B"]).limit(20000).execute().data
    from app.services.warmup import seed_emails

    seeds = seed_emails(campaign["user_id"])  # test inboxes get the campaign, never the follow-ups
    latest: dict[str, dict] = {}
    for log in sorted(logs, key=lambda l: l["sent_at"]):
        if log["recipient"].lower() not in seeds:
            latest[log["recipient"].lower()] = log
    anchors = [{"recipient": r, "email_id": l["id"], "sent_at": l["sent_at"]} for r, l in latest.items()]
    queued = queue_step(campaign, 1, anchors)
    if queued:
        logger.info("Queued %d follow-ups for campaign %s", queued, campaign["id"])


def recipient_states(user_id: str, campaign_id: str, recipients: list[str]) -> dict[str, dict]:
    """Everything stop_reason needs, for a batch of one campaign's recipients."""
    db = _db()
    first = db.table("email_logs").select("sent_at").eq("campaign_id", campaign_id).order("sent_at").limit(1).execute().data
    since = _utc(first[0]["sent_at"]) if first else datetime.now(timezone.utc)
    states = {r: {} for r in recipients}
    events = db.table("email_events").select("recipient, event_type, occurred_at").eq("user_id", user_id) \
        .in_("recipient", recipients).in_("event_type", ["replied", "bounced", "complained"]).execute().data
    for e in events:
        s = states.setdefault(e["recipient"].lower(), {})
        if e["event_type"] == "replied":
            s["replied"] = s.get("replied") or _utc(e["occurred_at"]) >= since  # a reply to this campaign, not an older one
        else:
            s[e["event_type"]] = True  # a bounce or complaint at any time stops all mail to them
    for lead in db.table("leads").select("email, lifecycle_stage, score_band").eq("user_id", user_id).in_("email", recipients).execute().data:
        states.setdefault(lead["email"], {}).update(stage=lead["lifecycle_stage"], band=lead["score_band"])
    return states


# ---------- Sending ----------

def _claim(ids: list[str]) -> list[dict]:
    """pending -> sending, atomically, so two scheduler ticks never send the same follow-up."""
    return _db().table("follow_up_sends").update({"status": "sending"}).in_("id", ids).eq("status", "pending").execute().data


def _finish(row_id: str, **fields):
    _db().table("follow_up_sends").update(fields).eq("id", row_id).execute()


def _skip_later_steps(campaign_id: str, recipient: str, step: int):
    """Later steps are only queued after a send, so this just guards rows queued by hand or by an older version."""
    _db().table("follow_up_sends").update({"status": "skipped", "skip_reason": STOP_REASONS["earlier_step"]}) \
        .eq("campaign_id", campaign_id).eq("recipient", recipient).gt("step", step).eq("status", "pending").execute()


def run_due(now: datetime | None = None) -> dict:
    """Called by the scheduler: sends due follow-ups, skipping anyone who should no longer get them."""
    from app.services.campaign_service import campaign_service

    now = now or datetime.now(timezone.utc)
    db = _db()
    due = db.table("follow_up_sends").select("id, campaign_id").eq("status", "pending").lte("due_at", now.isoformat()) \
        .order("due_at").limit(CLAIM_LIMIT).execute().data
    if not due:
        return {"sent": 0, "skipped": 0, "failed": 0}
    campaigns = {c["id"]: c for c in db.table("campaigns").select("*").in_("id", sorted({d["campaign_id"] for d in due})).execute().data}
    # A paused campaign keeps its follow-ups waiting until it's resumed
    ready = [d["id"] for d in due if campaigns.get(d["campaign_id"], {}).get("status") != "paused"]
    claimed = _claim(ready) if ready else []
    totals = {"sent": 0, "skipped": 0, "failed": 0}

    by_campaign: dict[str, list[dict]] = defaultdict(list)
    for row in claimed:
        by_campaign[row["campaign_id"]].append(row)
    for campaign_id, rows in by_campaign.items():
        c = campaigns[campaign_id]
        steps = {s["id"]: s for s in db.table("campaign_follow_ups").select("*").eq("campaign_id", campaign_id).execute().data}
        recipients = sorted({r["recipient"] for r in rows})
        states = recipient_states(c["user_id"], campaign_id, recipients)
        people = {p["email"]: p for p in db.table("campaign_recipients").select("email, name, variables").eq("campaign_id", campaign_id)
                  .in_("email", recipients).execute().data}
        ctx = None
        for row in rows:
            reason = stop_reason(states.get(row["recipient"], {}))
            if reason:
                _finish(row["id"], status="skipped", skip_reason=reason)
                _skip_later_steps(campaign_id, row["recipient"], row["step"])
                totals["skipped"] += 1
                continue
            step = steps[row["follow_up_id"]]
            person = {**people.get(row["recipient"], {"email": row["recipient"], "name": None, "variables": {}}), "variant": f"F{row['step']}"}
            message_campaign = {**c, "subject_a": step["subject"], "body_a": step["body"], "ab_enabled": False}
            message = campaign_service._build_message(message_campaign, {**person, "variant": "A"}, own_content=False)
            (result,) = campaign_service._send_chunk([message])
            log = campaign_service._log_rows(c, [person], [message], [result])[0]
            db.table("email_logs").insert(log).execute()
            if result[0] == "failed":
                _finish(row["id"], status="failed", skip_reason=(result[2] or "Send failed")[:300], email_id=log["id"])
                totals["failed"] += 1
                continue
            _finish(row["id"], status="sent", email_id=log["id"], sent_at=log["sent_at"])
            totals["sent"] += 1
            if row["step"] < MAX_STEPS:
                ctx = ctx or timing_context(c["user_id"], _default_tz(c))
                queue_step(c, row["step"] + 1, [{"recipient": row["recipient"], "email_id": log["id"], "sent_at": log["sent_at"]}], ctx)
    if any(totals.values()):
        print(f"[follow-ups] sent {totals['sent']}, skipped {totals['skipped']}, failed {totals['failed']}", flush=True)
    return totals


def recover_interrupted():
    """A follow-up left 'sending' by a restart may or may not have gone out; don't risk sending it twice."""
    _db().table("follow_up_sends").update({"status": "failed", "skip_reason": "Interrupted by a server restart"}).eq("status", "sending").execute()


# ---------- For the API ----------

def summary_for(campaign_ids: list[str]) -> dict[str, dict]:
    """Per campaign: number of steps and follow-ups by status."""
    if not campaign_ids:
        return {}
    db = _db()
    out: dict[str, dict] = {cid: {"steps": 0, "pending": 0, "sent": 0, "skipped": 0, "failed": 0, "cancelled": 0} for cid in campaign_ids}
    for s in db.table("campaign_follow_ups").select("campaign_id").in_("campaign_id", campaign_ids).execute().data:
        out[s["campaign_id"]]["steps"] += 1
    for r in db.table("follow_up_sends").select("campaign_id, status").in_("campaign_id", campaign_ids).limit(50000).execute().data:
        key = "pending" if r["status"] == "sending" else r["status"]
        out[r["campaign_id"]][key] += 1
    return out


def detail(user_id: str, campaign_id: str) -> dict:
    db = _db()
    steps = db.table("campaign_follow_ups").select("step, wait_days, subject").eq("campaign_id", campaign_id).eq("user_id", user_id).order("step").execute().data
    rows = db.table("follow_up_sends").select("step, recipient, due_at, status, skip_reason, sent_at").eq("campaign_id", campaign_id) \
        .eq("user_id", user_id).order("due_at").limit(500).execute().data
    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["status"] == "skipped" and r["skip_reason"]:
            reasons[r["skip_reason"]] += 1
    return {"steps": steps, "sends": rows, "skip_reasons": dict(reasons), "counts": summary_for([campaign_id]).get(campaign_id, {})}


def _blocked(user_id: str, rows: list[dict], recipient: str) -> str | None:
    """Why the waiting follow-ups would be skipped right now (replied, bounced, stage...), or None."""
    for campaign_id in dict.fromkeys(r["campaign_id"] for r in rows):
        state = recipient_states(user_id, campaign_id, [recipient.lower()]).get(recipient.lower(), {})
        reason = stop_reason(state)
        if reason:
            return reason
    return None


def _pending_rows(user_id: str, recipient: str) -> list[dict]:
    return _db().table("follow_up_sends").select("due_at, campaign_id").eq("user_id", user_id).eq("recipient", recipient)         .eq("status", "pending").order("due_at").execute().data


def waiting_for(user_id: str, recipient: str) -> dict:
    """This contact's follow-ups that haven't gone out yet, when the next is due, and `blocked`: why they won't go out."""
    rows = _pending_rows(user_id, recipient)
    return {"pending": len(rows), "next_due_at": rows[0]["due_at"] if rows else None,
            "blocked": _blocked(user_id, rows, recipient) if rows else None}


def send_now_for(user_id: str, recipient: str) -> int:
    """Makes this contact's waiting follow-ups due now; the scheduler sends them within 30 seconds, with the usual
    stop checks still applied at send time. Returns 0 when they would only be skipped (e.g. the contact replied)."""
    rows = _pending_rows(user_id, recipient)
    if not rows or _blocked(user_id, rows, recipient):
        return 0
    done = _db().table("follow_up_sends").update({"due_at": datetime.now(timezone.utc).isoformat()})         .eq("user_id", user_id).eq("recipient", recipient).eq("status", "pending").execute().data
    return len(done)


def cancel(user_id: str, campaign_id: str) -> int:
    rows = _db().table("follow_up_sends").update({"status": "cancelled", "skip_reason": STOP_REASONS["cancelled"]}) \
        .eq("campaign_id", campaign_id).eq("user_id", user_id).eq("status", "pending").execute().data
    return len(rows)
