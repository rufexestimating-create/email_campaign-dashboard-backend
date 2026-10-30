"""Domain warm-up: a daily sending limit that grows week by week, plus seed inboxes for placement tests.

Mailbox providers trust a new domain when its volume grows gradually and people engage with what it sends. This
module only controls *how much* real mail goes out; it never creates fake engagement. Seed inboxes are the team's own
test addresses: they're added to every campaign so someone can look where each email landed and log it (those logs
train the spam model). Seeds are kept out of leads, lead scoring and follow-ups.
"""
from datetime import date, datetime, timedelta, timezone

from app.core.config import settings
from app.services.event_tracking import provider_for

# (week, emails per day). After the last week the warm-up cap ends and only the account's daily limit applies.
SCHEDULE = [(1, 20), (2, 40), (3, 75), (4, 150), (5, 300)]
SEED_LIMIT = 10


def _db():
    from app.db.supabase import get_supabase

    return get_supabase()


# ---------- Pure ----------

def plan(start: date, today: date, enabled: bool, account_limit: int) -> dict:
    """Where the domain is in its warm-up, today's limit, and the whole schedule with dates."""
    day = (today - start).days
    week = max(1, day // 7 + 1)
    rows = []
    for w, per_day in SCHEDULE:
        rows.append({"week": w, "from": (start + timedelta(days=7 * (w - 1))).isoformat(), "per_day": min(per_day, account_limit),
                     "current": enabled and w == week})
    rows.append({"week": len(SCHEDULE) + 1, "from": (start + timedelta(days=7 * len(SCHEDULE))).isoformat(), "per_day": account_limit,
                 "current": enabled and week > len(SCHEDULE), "full": True})
    if not enabled or day < 0:
        return {"enabled": enabled, "week": None, "limit": account_limit, "warming": False, "schedule": rows}
    warming = week <= len(SCHEDULE)
    limit = min(dict(SCHEDULE)[week], account_limit) if warming else account_limit
    return {"enabled": True, "week": week, "limit": limit, "warming": warming, "schedule": rows,
            "days_left": max(0, 7 * len(SCHEDULE) - day) if warming else 0}


# ---------- Settings and seeds ----------

def get_settings(user_id: str) -> dict:
    rows = _db().table("warmup_settings").select("*").eq("user_id", user_id).limit(1).execute().data
    if rows:
        return rows[0]
    # First visit: start the warm-up from the first email this account ever sent (or today)
    first = _db().table("email_logs").select("sent_at").eq("user_id", user_id).in_("status", ["sent"]).order("sent_at").limit(1).execute().data
    start = datetime.fromisoformat(first[0]["sent_at"]).date() if first else date.today()
    return {"user_id": user_id, "enabled": True, "start_date": start.isoformat(), "seeds_enabled": True}


def update_settings(user_id: str, changes: dict) -> dict:
    row = {**get_settings(user_id), **{k: v for k, v in changes.items() if v is not None}, "updated_at": datetime.now(timezone.utc).isoformat()}
    if isinstance(row["start_date"], date):
        row["start_date"] = row["start_date"].isoformat()
    return _db().table("warmup_settings").upsert(row, on_conflict="user_id").execute().data[0]


def list_seeds(user_id: str) -> list[dict]:
    return _db().table("seed_inboxes").select("id, email, provider, created_at").eq("user_id", user_id).order("created_at").execute().data


def seed_emails(user_id: str) -> set[str]:
    try:
        return {s["email"] for s in list_seeds(user_id)}
    except Exception:  # table missing or database hiccup: seeds are optional
        return set()


def add_seed(user_id: str, email: str) -> dict:
    email = email.strip().lower()
    if len(list_seeds(user_id)) >= SEED_LIMIT:
        raise ValueError(f"You can have up to {SEED_LIMIT} seed inboxes.")
    return _db().table("seed_inboxes").upsert({"user_id": user_id, "email": email, "provider": provider_for(email)},
                                              on_conflict="user_id,email").execute().data[0]


def remove_seed(user_id: str, seed_id: str) -> bool:
    return bool(_db().table("seed_inboxes").delete().eq("id", seed_id).eq("user_id", user_id).execute().data)


def seed_recipients(user_id: str) -> list[dict]:
    """Recipients to add to a campaign: the seed inboxes, when the user has them switched on."""
    try:
        if not get_settings(user_id).get("seeds_enabled", True):
            return []
        return [{"email": s["email"], "name": None, "variables": {"seed_inbox": "1"}} for s in list_seeds(user_id)]
    except Exception:
        return []


# ---------- Today's limit ----------

def sent_today(user_id: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = _db().table("email_logs").select("id", count="exact").eq("user_id", user_id).eq("status", "sent").gte("sent_at", start).limit(1).execute()
    return res.count or 0


def today_status(user_id: str) -> dict:
    s = get_settings(user_id)
    p = plan(date.fromisoformat(str(s["start_date"])[:10]), date.today(), s["enabled"], settings.DAILY_SEND_LIMIT)
    sent = sent_today(user_id)
    return {**p, "sent_today": sent, "remaining_today": max(p["limit"] - sent, 0), "account_limit": settings.DAILY_SEND_LIMIT,
            "start_date": str(s["start_date"])[:10], "seeds_enabled": s.get("seeds_enabled", True)}


def daily_limit(user_id: str) -> tuple[int, int | None]:
    """(today's limit, warm-up week or None) for the pre-send volume check. Falls back to the account limit."""
    try:
        s = get_settings(user_id)
        p = plan(date.fromisoformat(str(s["start_date"])[:10]), date.today(), s["enabled"], settings.DAILY_SEND_LIMIT)
        return p["limit"], (p["week"] if p["warming"] else None)
    except Exception:
        return settings.DAILY_SEND_LIMIT, None
