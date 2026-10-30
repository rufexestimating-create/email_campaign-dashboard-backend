"""Send-time and behaviour analysis (M6).

Engagement is measured from clicks and replies only. Opens are left out: Apple Mail Privacy Protection and the Gmail
and Yahoo image proxies "open" emails automatically, at times that say nothing about when a person reads them.

Every event is converted to the recipient's local time (leads.timezone, from ZIP/state) before it is bucketed, so a
9 AM click in Austin and a 9 AM click in New York land in the same hour. Recommendations fall back in this order:
  1. the lead's own profile, once they have MIN_LEAD_EVENTS clicks/replies
  2. what their persona has done, once the persona has MIN_SEGMENT_EVENTS
  3. a default window for the persona (cold start)
Follow-ups: if a contact's replies usually arrive SLOW_REPLY_HOURS or more after the email, wait 24-48 hours before
following up instead of chasing them the same day.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from app.services.local_time import is_valid_timezone, to_local

ENGAGEMENT_WEIGHT = {"clicked": 1, "replied": 3}  # a reply is a much stronger sign of attention than a click
PERSONAS = ("contractor", "architect", "homeowner", "other")
DEVICES = ("desktop", "mobile", "tablet", "unknown")
MIN_LEAD_EVENTS = 3
CONFIDENT_LEAD_EVENTS = 5
MIN_SEGMENT_EVENTS = 20
SLOW_REPLY_HOURS = 3
VERY_SLOW_REPLY_HOURS = 12
WINDOW_HOURS = 2
BEST_DAY_SHARE = 0.6  # a weekday joins the window when it has at least 60% of the busiest day's engagement
MIN_GAP_MINUTES = 15
DEFAULT_TIMEZONE = "America/New_York"  # most US contacts are on Eastern time; used when a lead has no ZIP/state
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Cold-start windows (recipient local time), from published B2B / residential remodel engagement benchmarks
DEFAULT_WINDOWS = {
    "contractor": {"days": (0, 1, 2, 3), "start": 6, "end": 8,
                   "why": "Contractors usually read email early, before they head out to site."},
    "architect": {"days": (1, 2, 3), "start": 9, "end": 11,
                  "why": "Architects and designers usually deal with email mid-morning, between studio meetings."},
    "homeowner": {"days": (1, 2, 3), "start": 19, "end": 21,
                  "why": "Homeowners usually read personal email in the evening, after work."},
    "other": {"days": (1, 2, 3), "start": 8, "end": 11,
              "why": "Business contacts read email most on Tuesday to Thursday mornings."},
}


# ---------- Building blocks ----------

def _utc(value) -> datetime:
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _persona(lead: dict | None) -> str:
    return (lead or {}).get("persona") or "other"


def _tz(lead: dict | None, default_tz: str) -> tuple[str, bool]:
    """(timezone, known) for a lead; unknown zones fall back to the default."""
    tz = (lead or {}).get("timezone")
    return (tz, True) if is_valid_timezone(tz) else (default_tz, False)


def engagement_rows(events: list[dict], leads: dict[str, dict], default_tz: str = DEFAULT_TIMEZONE) -> list[dict]:
    """Clicks and replies with the recipient's persona, device and local weekday/hour."""
    rows = []
    for e in events:
        weight = ENGAGEMENT_WEIGHT.get(e["event_type"])
        if not weight:
            continue
        recipient = e["recipient"].lower()
        lead = leads.get(recipient)
        tz, known = _tz(lead, default_tz)
        local = to_local(_utc(e["occurred_at"]), tz)
        device = e.get("device") if e.get("device") in DEVICES else "unknown"  # proxy / missing = unknown
        rows.append({
            "recipient": recipient, "kind": e["event_type"], "weight": weight, "persona": _persona(lead), "device": device,
            "weekday": local.weekday(), "hour": local.hour, "timezone": tz, "timezone_known": known,
        })
    return rows


def empty_grid() -> list[list[float]]:
    return [[0] * 24 for _ in range(7)]


def grid_of(rows: list[dict]) -> list[list[float]]:
    """7 x 24 (Monday first, local hours) of weighted clicks + replies."""
    grid = empty_grid()
    for r in rows:
        grid[r["weekday"]][r["hour"]] += r["weight"]
    return grid


def best_window(grid: list[list[float]]) -> dict | None:
    """Busiest weekdays and the best WINDOW_HOURS-hour block on them."""
    day_totals = [sum(day) for day in grid]
    top = max(day_totals)
    if top <= 0:
        return None
    busy = [d for d in range(7) if day_totals[d] >= top * BEST_DAY_SHARE]
    days = sorted(sorted(busy, key=lambda d: -day_totals[d])[:3])
    block = lambda h: sum(grid[d][h + i] for d in days for i in range(WINDOW_HOURS))
    start = max(range(24 - WINDOW_HOURS + 1), key=lambda h: (block(h), -abs(h - 10)))  # ties go to the hour nearest 10 AM
    return {"days": tuple(days), "start": start, "end": start + WINDOW_HOURS}


def window_label(window: dict) -> str:
    days = window["days"]
    consecutive = len(days) > 2 and list(days) == list(range(days[0], days[-1] + 1))
    day_text = f"{DAY_SHORT[days[0]]}–{DAY_SHORT[days[-1]]}" if consecutive else ", ".join(DAY_SHORT[d] for d in days)
    hour = lambda h: datetime(2000, 1, 1, h % 24).strftime("%I %p").lstrip("0")
    return f"{day_text}, {hour(window['start'])}–{hour(window['end'])}"


def in_window(local: datetime, window: dict) -> bool:
    return local.weekday() in window["days"] and window["start"] <= local.hour < window["end"]


def next_window_start(now_utc: datetime, tz: str, window: dict) -> datetime:
    """Next start of the window in the recipient's timezone, at least MIN_GAP_MINUTES from now (aware, local)."""
    local_now = to_local(now_utc, tz)
    candidate = local_now.replace(hour=window["start"], minute=0, second=0, microsecond=0)
    for _ in range(8):
        if candidate.weekday() in window["days"] and candidate > local_now + timedelta(minutes=MIN_GAP_MINUTES):
            return candidate
        candidate += timedelta(days=1)
    return candidate


# ---------- Heatmaps, profiles, reply delays ----------

def heatmaps(rows: list[dict]) -> dict:
    """persona x device x weekday x hour. "all" rows/columns are the totals; empty combinations are left out."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        for persona in (r["persona"], "all"):
            for device in (r["device"], "all"):
                groups[(persona, device)].append(r)
    out: dict[str, dict] = {}
    for (persona, device), rs in sorted(groups.items()):
        out.setdefault(persona, {})[device] = {
            "grid": grid_of(rs),
            "clicks": sum(r["kind"] == "clicked" for r in rs),
            "replies": sum(r["kind"] == "replied" for r in rs),
        }
    return out


def lead_profiles(rows: list[dict]) -> dict[str, dict]:
    """Per-contact best window once they have MIN_LEAD_EVENTS clicks/replies."""
    by_lead: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_lead[r["recipient"]].append(r)
    profiles = {}
    for recipient, rs in by_lead.items():
        if len(rs) < MIN_LEAD_EVENTS:
            continue
        window = best_window(grid_of(rs))
        devices = Counter(r["device"] for r in rs if r["device"] != "unknown")
        profiles[recipient] = {
            "events": len(rs),
            "confidence": "good" if len(rs) >= CONFIDENT_LEAD_EVENTS else "low",
            "window": window,
            "window_label": window_label(window),
            "main_device": devices.most_common(1)[0][0] if devices else None,
            "timezone": rs[0]["timezone"],
        }
    return profiles


def segment_windows(rows: list[dict]) -> dict[str, dict]:
    """Learned window per persona (and "all") once there is enough engagement to trust it."""
    by_persona: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_persona[r["persona"]].append(r)
        by_persona["all"].append(r)
    return {
        persona: {"window": best_window(grid_of(rs)), "events": len(rs)}
        for persona, rs in by_persona.items() if len(rs) >= MIN_SEGMENT_EVENTS
    }


def follow_up_after_hours(median_reply_hours: float | None) -> int | None:
    """The reply-delay rule. None = no extra wait needed (or no replies to learn from)."""
    if median_reply_hours is None or median_reply_hours < SLOW_REPLY_HOURS:
        return None
    return 48 if median_reply_hours >= VERY_SLOW_REPLY_HOURS else 24


def reply_delays(replies: list[dict], sent_at: dict[str, str]) -> dict:
    """Hours from send to reply, per contact and overall. `sent_at` maps email_logs.id -> sent_at."""
    per_lead: dict[str, list[float]] = defaultdict(list)
    for r in replies:
        sent = sent_at.get(r.get("email_id") or "")
        if not sent:
            continue
        hours = (_utc(r["occurred_at"]) - _utc(sent)).total_seconds() / 3600
        if hours >= 0:
            per_lead[r["recipient"].lower()].append(hours)
    every = [h for hs in per_lead.values() for h in hs]
    overall = round(median(every), 1) if every else None
    return {
        "replies_measured": len(every),
        "median_hours": overall,
        "follow_up_after_hours": follow_up_after_hours(overall),
        "per_lead": {
            email: {"replies": len(hs), "median_hours": round(median(hs), 1), "follow_up_after_hours": follow_up_after_hours(median(hs))}
            for email, hs in per_lead.items()
        },
    }


# ---------- Recommendations ----------

def window_for(recipient: str, lead: dict | None, profiles: dict, segments: dict) -> dict:
    """Best window for one contact, with where it came from."""
    profile = profiles.get(recipient)
    if profile:
        return {"window": profile["window"], "source": "lead_profile", "events": profile["events"], "confidence": profile["confidence"],
                "why": f"Learned from this contact's own {profile['events']} clicks and replies."}
    persona = _persona(lead)
    learned = segments.get(persona)
    if learned:
        name = "contacts" if persona == "other" else f"{persona}s"
        return {"window": learned["window"], "source": "persona_history", "events": learned["events"], "confidence": "good",
                "why": f"Learned from {learned['events']} clicks and replies by your {name}."}
    default = DEFAULT_WINDOWS[persona]
    return {"window": {k: default[k] for k in ("days", "start", "end")}, "source": "default", "events": 0, "confidence": "default",
            "why": f"Default until there is enough data: {default['why']}"}


def plan_recipient(recipient: str, lead: dict | None, profiles: dict, segments: dict, delays: dict,
                   now_utc: datetime, default_tz: str = DEFAULT_TIMEZONE) -> dict:
    tz, known = _tz(lead, default_tz)
    choice = window_for(recipient, lead, profiles, segments)
    # Inside their window right now: now is the best time, not the next window
    open_now = in_window(to_local(now_utc, tz), choice["window"])
    at = to_local(now_utc, tz) if open_now else next_window_start(now_utc, tz, choice["window"])
    lead_delay = delays["per_lead"].get(recipient)
    follow_up = lead_delay["follow_up_after_hours"] if lead_delay else delays["follow_up_after_hours"]
    return {
        "recipient": recipient,
        "persona": _persona(lead),
        "timezone": tz,
        "timezone_known": known,
        "window": window_label(choice["window"]),
        "source": choice["source"],
        "confidence": choice["confidence"],
        "why": choice["why"],
        "open_now": open_now,
        "send_at_local": at.isoformat(),
        "send_at_utc": at.astimezone(timezone.utc).isoformat(),
        "follow_up_after_hours": follow_up,
        "follow_up_note": (f"Replies usually take {lead_delay['median_hours'] if lead_delay else delays['median_hours']} hours; "
                           f"wait {follow_up} hours before following up.") if follow_up else None,
    }


def timing_check(recipients: list[str], leads: dict[str, dict], profiles: dict, segments: dict,
                 planned_utc: datetime, now_utc: datetime, default_tz: str = DEFAULT_TIMEZONE) -> dict:
    """How many recipients the planned send reaches inside their own best window, and a better time if needed."""
    hits, groups = 0, Counter()
    choices = {}
    for r in recipients:
        lead = leads.get(r)
        tz, _ = _tz(lead, default_tz)
        choice = window_for(r, lead, profiles, segments)
        choices[r] = choice
        hits += in_window(to_local(planned_utc, tz), choice["window"])
        w = choice["window"]
        groups[(w["days"], w["start"], w["end"], tz)] += 1
    if not recipients:
        return {"in_window": 0, "total": 0, "good": True, "learned": False, "best_label": None, "reason": None, "suggestion": None}

    (days, start, end, tz), _ = groups.most_common(1)[0]
    main = {"days": days, "start": start, "end": end}
    sources = Counter(c["source"] for c in choices.values())
    learned = sources["default"] < len(recipients)
    reason = next(c["why"] for c in choices.values() if c["window"] == main)
    return {
        "in_window": hits,
        "total": len(recipients),
        "good": hits / len(recipients) >= 0.5,
        "learned": learned,
        "sources": dict(sources),
        "best_label": f"{window_label(main)} ({tz.split('/')[-1].replace('_', ' ')} time)",
        "reason": reason,
        "suggestion": next_window_start(now_utc, tz, main),
        "main_timezone": tz,
    }


# ---------- Loading from Supabase ----------

def _pages(query, limit: int = 20_000) -> list[dict]:
    rows, start = [], 0
    while len(rows) < limit:
        page = query.range(start, start + 999).execute().data
        rows.extend(page)
        if len(page) < 1000:
            break
        start += 1000
    return rows


def load_leads(user_id: str) -> dict[str, dict]:
    from app.db.supabase import get_supabase

    rows = _pages(get_supabase().table("leads").select("email, persona, timezone").eq("user_id", user_id))
    return {r["email"].lower(): r for r in rows}


def load_engagement(user_id: str, days: int) -> list[dict]:
    from app.db.supabase import get_supabase

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return _pages(
        get_supabase().table("email_events").select("recipient, email_id, event_type, occurred_at, device")
        .eq("user_id", user_id).in_("event_type", list(ENGAGEMENT_WEIGHT)).gte("occurred_at", since).order("occurred_at")
    )


def load_sent_at(user_id: str, email_ids: list[str]) -> dict[str, str]:
    from app.db.supabase import get_supabase

    out = {}
    ids = sorted(set(email_ids))
    for i in range(0, len(ids), 200):
        for row in get_supabase().table("email_logs").select("id, sent_at").eq("user_id", user_id).in_("id", ids[i:i + 200]).execute().data:
            out[row["id"]] = row["sent_at"]
    return out


def _learn(user_id: str, days: int, default_tz: str, leads: dict[str, dict]) -> tuple[list[dict], list[dict], dict]:
    events = load_engagement(user_id, days)
    replies = [e for e in events if e["event_type"] == "replied"]
    delays = reply_delays(replies, load_sent_at(user_id, [r["email_id"] for r in replies if r.get("email_id")]))
    return events, engagement_rows(events, leads, default_tz), delays


def _window_of(persona: str, segments: dict) -> dict:
    """The learned window for a persona, or its default."""
    if persona in segments:
        return segments[persona]["window"]
    return {k: DEFAULT_WINDOWS[persona][k] for k in ("days", "start", "end")}


def analyze(user_id: str, days: int = 180, default_tz: str = DEFAULT_TIMEZONE) -> dict:
    leads = load_leads(user_id)
    events, rows, delays = _learn(user_id, days, default_tz, leads)
    segments = segment_windows(rows)
    profiles = lead_profiles(rows)
    return {
        "days": days,
        "engagement_events": len(rows),
        "clicks": sum(r["kind"] == "clicked" for r in rows),
        "replies": sum(r["kind"] == "replied" for r in rows),
        "timezone_assumed": sum(not r["timezone_known"] for r in rows),
        "default_timezone": default_tz,
        "heatmaps": heatmaps(rows),
        "windows": {
            persona: {
                "learned": persona in segments,
                "label": window_label(_window_of(persona, segments)),
                "events": segments.get(persona, {}).get("events", 0),
                "why": DEFAULT_WINDOWS[persona]["why"] if persona not in segments else None,
                # When to send next in the default timezone, so the page can say "Next: Thu 30 Oct, 8 AM" (or "now")
                "open_now": in_window(to_local(datetime.now(timezone.utc), default_tz), _window_of(persona, segments)),
                "next_at": next_window_start(datetime.now(timezone.utc), default_tz, _window_of(persona, segments)).isoformat(),
            }
            for persona in PERSONAS
        },
        "lead_profiles": profiles,
        "reply_delay": {k: v for k, v in delays.items() if k != "per_lead"},
        "thresholds": {"lead_profile_events": MIN_LEAD_EVENTS, "persona_events": MIN_SEGMENT_EVENTS, "slow_reply_hours": SLOW_REPLY_HOURS},
        "note": "Built from clicks and replies in each recipient's local time. Opens are not used because privacy proxies open emails automatically.",
    }


def plan(user_id: str, recipients: list[str], days: int = 180, default_tz: str = DEFAULT_TIMEZONE, now_utc: datetime | None = None) -> list[dict]:
    emails = sorted({e.strip().lower() for e in recipients if e and "@" in e})
    all_leads = load_leads(user_id)
    _, rows, delays = _learn(user_id, days, default_tz, all_leads)
    profiles, segments = lead_profiles(rows), segment_windows(rows)
    now = now_utc or datetime.now(timezone.utc)
    return [plan_recipient(e, all_leads.get(e), profiles, segments, delays, now, default_tz) for e in emails]


def precheck_timing(user_id: str, recipients: list[str], planned_utc: datetime, days: int = 180, default_tz: str = DEFAULT_TIMEZONE) -> dict:
    all_leads = load_leads(user_id)
    _, rows, _ = _learn(user_id, days, default_tz, all_leads)
    return timing_check(recipients, all_leads, lead_profiles(rows), segment_windows(rows), planned_utc, datetime.now(timezone.utc), default_tz)
