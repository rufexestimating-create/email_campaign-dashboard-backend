"""Email events from Resend webhooks, replies, placement labels and lead profiles.

Resend signs every webhook with Svix: HMAC-SHA256 of "<svix-id>.<svix-timestamp>.<raw body>" using the
endpoint's signing secret (whsec_...). Unsigned or replayed requests are rejected before anything is stored.
"""
import base64
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone

from postgrest.exceptions import APIError

from app.db.supabase import get_supabase
from app.services.local_time import clean_state, clean_zip, is_valid_timezone, state_for_zip, timezone_for

SIGNATURE_TOLERANCE_SECONDS = 5 * 60
EVENT_TYPES = {"sent", "delivered", "delivery_delayed", "opened", "clicked", "replied", "bounced", "complained", "failed"}
# email_logs.last_event only moves forward, so a late "delivered" never hides an earlier "clicked"
EVENT_RANK = {"sent": 0, "delivery_delayed": 1, "delivered": 2, "opened": 3, "clicked": 4, "replied": 5, "failed": 6, "bounced": 7, "complained": 8}
PERSONAS = ("contractor", "architect", "homeowner", "other")
PROVIDERS = {
    "gmail": ("gmail.com", "googlemail.com"),
    "outlook": ("outlook.com", "hotmail.com", "live.com", "msn.com"),
    "yahoo": ("yahoo.com", "ymail.com", "aol.com", "rocketmail.com"),
    "icloud": ("icloud.com", "me.com", "mac.com"),
}
UNIQUE_VIOLATION = "23505"


class WebhookSignatureError(Exception):
    pass


def verify_signature(secret: str, headers: dict, body: bytes, now: float | None = None) -> None:
    """Raises WebhookSignatureError unless the request was signed with `secret` in the last 5 minutes."""
    msg_id, stamp, signatures = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not (msg_id and stamp and signatures):
        raise WebhookSignatureError("Missing signature headers")
    try:
        sent_at = int(stamp)
    except ValueError:
        raise WebhookSignatureError("Bad timestamp")
    if abs((now if now is not None else time.time()) - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        raise WebhookSignatureError("Timestamp too old or in the future")
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        raise WebhookSignatureError("Signing secret is not valid base64")
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in signatures.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return
    raise WebhookSignatureError("Signature does not match")


def device_from_user_agent(ua: str | None) -> str | None:
    """Rough device class. 'proxy' = Gmail/Yahoo image proxies, which hide the real device."""
    if not ua:
        return None
    u = ua.lower()
    if "googleimageproxy" in u or "ggpht.com" in u or "yahoomailproxy" in u:
        return "proxy"
    if "ipad" in u or "tablet" in u or ("android" in u and "mobile" not in u):
        return "tablet"
    if any(k in u for k in ("iphone", "ipod", "android", "mobile", "windows phone")):
        return "mobile"
    return "desktop"


def provider_for(email: str) -> str:
    domain = email.rsplit("@", 1)[-1].lower()
    return next((p for p, domains in PROVIDERS.items() if domain in domains), "other")


def normalize_persona(value: str | None) -> str | None:
    v = (value or "").strip().lower()
    if not v:
        return None
    if re.search(r"contractor|\bgc\b|builder|remodel", v):
        return "contractor"
    if re.search(r"architect|designer", v):
        return "architect"
    if re.search(r"home\s*-?\s*owner|resident", v):
        return "homeowner"
    return "other"


def as_utc(dt: datetime) -> datetime:
    """Naive datetimes are taken as UTC."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_time(value) -> datetime | None:
    """ISO-8601 (with 'Z' or an offset) -> aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _lead_id(db, user_id: str, email: str) -> str | None:
    try:
        rows = db.table("leads").select("id").eq("user_id", user_id).eq("email", email.lower()).limit(1).execute().data
    except APIError:
        return None
    return rows[0]["id"] if rows else None


def _advance_last_event(db, log: dict, kind: str):
    """Moves last_event forward only. The rank check runs in the database, not on the row read earlier: Resend sends
    "sent" and "delivered" within the same second, and two webhooks handled at once would otherwise let the slower
    "sent" overwrite "delivered"."""
    rank = EVENT_RANK.get(kind, -1)
    if rank <= EVENT_RANK.get(log.get("last_event") or "", -1):
        return
    lower = ",".join(k for k, r in EVENT_RANK.items() if r < rank)
    allowed = f"last_event.is.null,last_event.in.({lower})" if lower else "last_event.is.null"
    db.table("email_logs").update({"last_event": kind}).eq("id", log["id"]).or_(allowed).execute()


def _auto_label(db, log: dict, kind: str, occurred: datetime, bounce_type: str | None):
    """Complaints and bounces are the only placement signals Resend gives us, so each becomes a label."""
    try:
        db.table("placement_labels").insert({
            "user_id": log["user_id"], "email_id": log["id"], "campaign_id": log.get("campaign_id"),
            "recipient": log["recipient"], "provider": provider_for(log["recipient"]),
            "placement": kind, "source": "complaint" if kind == "complained" else "bounce",
            "observed_at": occurred.isoformat(), "notes": f"Bounce type: {bounce_type}" if bounce_type else None,
        }).execute()
    except APIError as e:
        if e.code != UNIQUE_VIOLATION:  # already labelled by an earlier delivery of this webhook
            raise


def handle_resend_event(payload: dict, svix_id: str | None) -> str:
    """Stores one webhook event. Returns what happened, for the response and the logs."""
    kind = str(payload.get("type") or "").removeprefix("email.")
    if kind not in EVENT_TYPES:
        return "ignored"
    data = payload.get("data") or {}
    db = get_supabase()
    logs = (
        db.table("email_logs").select("id, user_id, recipient, campaign_id, variant, last_event")
        .eq("id", str(data.get("email_id") or "")).limit(1).execute().data
    )
    if not logs:
        return "unknown_email"  # e.g. a verification email, which isn't logged
    log = logs[0]
    click = data.get("click") or {}
    open_ = data.get("open") or {}
    bounce = data.get("bounce") or {}
    user_agent = click.get("userAgent") or open_.get("userAgent")
    occurred = parse_time(click.get("timestamp") or open_.get("timestamp") or payload.get("created_at")) or datetime.now(timezone.utc)

    try:
        db.table("email_events").insert({
            "user_id": log["user_id"], "email_id": log["id"], "campaign_id": log.get("campaign_id"),
            "variant": log.get("variant"), "recipient": log["recipient"], "lead_id": _lead_id(db, log["user_id"], log["recipient"]),
            "event_type": kind, "occurred_at": occurred.isoformat(),
            "device": device_from_user_agent(user_agent), "user_agent": (user_agent or None) and user_agent[:500],
            "link": (click.get("link") or None) and str(click["link"])[:2000], "bounce_type": bounce.get("type"),
            "source": "resend_webhook", "svix_id": svix_id, "data": data,
        }).execute()
    except APIError as e:
        if e.code == UNIQUE_VIOLATION:
            return "duplicate"  # Resend retried a webhook we already stored
        raise

    _advance_last_event(db, log, kind)
    if kind in ("bounced", "complained"):
        _auto_label(db, log, kind, occurred, bounce.get("type"))
    return "stored"


def _latest_log(db, user_id: str, recipient: str, campaign_id: str | None, email_id: str | None) -> dict | None:
    q = db.table("email_logs").select("id, user_id, recipient, campaign_id, variant, last_event").eq("user_id", user_id)
    if email_id:
        q = q.eq("id", email_id)
    else:
        q = q.eq("recipient", recipient.lower()).in_("status", ["sent", "simulated"])
        if campaign_id:
            q = q.eq("campaign_id", campaign_id)
    rows = q.order("sent_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def record_reply(user_id: str, recipient: str, replied_at: datetime | None, campaign_id: str | None = None) -> dict | None:
    """Resend doesn't see replies (they go to your inbox), so they're recorded here. Attaches to the latest email sent to them."""
    db = get_supabase()
    log = _latest_log(db, user_id, recipient, campaign_id, None)
    if not log:
        return None
    when = as_utc(replied_at or datetime.now(timezone.utc))
    row = db.table("email_events").insert({
        "user_id": user_id, "email_id": log["id"], "campaign_id": log.get("campaign_id"), "variant": log.get("variant"),
        "recipient": log["recipient"], "lead_id": _lead_id(db, user_id, log["recipient"]), "event_type": "replied",
        "occurred_at": when.isoformat(), "source": "manual",
    }).execute().data[0]
    _advance_last_event(db, log, "replied")
    return row


def add_placement_label(user_id: str, data: dict) -> dict | None:
    """A seed-test or manual result: where did this email actually land?"""
    db = get_supabase()
    log = None
    if data.get("email_id") or data.get("campaign_id"):
        log = _latest_log(db, user_id, data["recipient"], data.get("campaign_id"), data.get("email_id"))
        if not log:
            return None
    return db.table("placement_labels").insert({
        "user_id": user_id, "email_id": log["id"] if log else None, "campaign_id": log.get("campaign_id") if log else None,
        "recipient": data["recipient"].lower(), "provider": provider_for(data["recipient"]), "placement": data["placement"],
        "source": data["source"], "observed_at": as_utc(data.get("observed_at") or datetime.now(timezone.utc)).isoformat(),
        "notes": data.get("notes"),
    }).execute().data[0]


# ---------- Leads ----------

def _pick(variables: dict, *keys: str) -> str | None:
    return next((str(variables[k]).strip() for k in keys if str(variables.get(k) or "").strip()), None)


def lead_fields(variables: dict) -> dict:
    """Profile fields found in a recipient's spreadsheet columns (keys are already lower_snake_case)."""
    zip_code = clean_zip(_pick(variables, "zip", "zip_code", "zipcode", "postal_code", "postcode"))
    state = clean_state(_pick(variables, "state", "st", "state_code")) or state_for_zip(zip_code)
    tz = _pick(variables, "timezone", "time_zone", "tz")
    return {k: v for k, v in {
        "company": _pick(variables, "company", "company_name", "organization", "organisation", "firm"),
        "persona": normalize_persona(_pick(variables, "persona", "role", "type", "contact_type", "audience", "segment")),
        "zip": zip_code,
        "state": state,
        "timezone": tz if is_valid_timezone(tz) else timezone_for(zip_code, state),
    }.items() if v}


def sync_leads(user_id: str, recipients: list[dict]) -> None:
    """Creates a lead for each new recipient and updates profile fields the spreadsheet provides."""
    db = get_supabase()
    by_email = {r["email"].lower(): r for r in recipients}
    existing: dict[str, dict] = {}
    emails = list(by_email)
    for i in range(0, len(emails), 200):
        for row in db.table("leads").select("id, email, name, company, persona, zip, state, timezone").eq("user_id", user_id).in_("email", emails[i:i + 200]).execute().data:
            existing[row["email"]] = row

    new_rows = []
    now = datetime.now(timezone.utc).isoformat()
    for email, r in by_email.items():
        fields = {**lead_fields(r.get("variables") or {}), **({"name": r["name"]} if r.get("name") else {})}
        if email not in existing:
            new_rows.append({"user_id": user_id, "email": email, "company": None, "persona": None, "zip": None, "state": None, "timezone": None, "name": None, **fields})
            continue
        changes = {k: v for k, v in fields.items() if existing[email].get(k) != v}
        if changes:
            db.table("leads").update({**changes, "updated_at": now}).eq("id", existing[email]["id"]).execute()
    for i in range(0, len(new_rows), 500):
        db.table("leads").upsert(new_rows[i:i + 500], on_conflict="user_id,email", ignore_duplicates=True).execute()


def update_lead(user_id: str, lead_id: str, changes: dict) -> dict | None:
    """Manual profile edit. A new ZIP or state re-derives the timezone unless one is given."""
    db = get_supabase()
    rows = db.table("leads").select("*").eq("id", lead_id).eq("user_id", user_id).limit(1).execute().data
    if not rows:
        return None
    patch = dict(changes)
    if "zip" in patch:
        patch["zip"] = clean_zip(patch["zip"])
    if "state" in patch:
        patch["state"] = clean_state(patch["state"])
    if patch.get("zip") and "state" not in patch:
        patch["state"] = state_for_zip(patch["zip"]) or rows[0]["state"]
    if ("zip" in patch or "state" in patch) and "timezone" not in patch:
        patch["timezone"] = timezone_for(patch.get("zip", rows[0]["zip"]), patch.get("state", rows[0]["state"]))
    now = datetime.now(timezone.utc).isoformat()
    if patch.get("lifecycle_stage") and patch["lifecycle_stage"] != rows[0].get("lifecycle_stage"):
        patch["stage_changed_at"] = now
    elif "lifecycle_stage" in patch and not patch["lifecycle_stage"]:
        del patch["lifecycle_stage"]  # the column can't be empty
    patch["updated_at"] = now
    return db.table("leads").update(patch).eq("id", lead_id).execute().data[0]
