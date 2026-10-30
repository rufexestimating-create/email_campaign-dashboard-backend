"""Pre-send recommendations: spam risk + the user's own sending history (bounces, fatigue, best send windows)."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.db.supabase import get_supabase
from app.services import placement_model, send_time
from app.services.campaign_service import upcoming_cron_runs
from app.services.spam_knowledge import ROLE_BASED_PREFIXES

HISTORY_DAYS = 90
FATIGUE_HOURS = 72
MIN_OPENS_FOR_LEARNING = 20
WARMUP_TOTAL = 200
WARMUP_BATCH = 50


def _rec(recs: list, id_: str, level: str, title: str, detail: str, action: dict | None = None):
    recs.append({"id": id_, "level": level, "title": title, "detail": detail, "action": action})


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _history(user_id: str) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    rows, start = [], 0
    while True:
        page = (
            get_supabase().table("email_logs")
            .select("recipient, status, last_event, sent_at")
            .eq("user_id", user_id)
            .gte("sent_at", since)
            .order("sent_at", desc=True)
            .range(start, start + 999)
            .execute()
            .data
        )
        rows.extend(page)
        if len(page) < 1000 or len(rows) >= 10_000:
            return rows
        start += 1000


def _planned_time(schedule: dict, tz: ZoneInfo) -> datetime | None:
    if schedule["type"] == "now":
        return datetime.now(tz)
    if schedule["type"] == "once" and schedule.get("send_at"):
        at = schedule["send_at"]
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return at.astimezone(tz)
    if schedule["type"] == "recurring" and schedule.get("cron"):
        try:
            return upcoming_cron_runs(schedule["cron"], schedule.get("timezone") or "UTC", 2)[0].astimezone(tz)
        except Exception:
            return None
    return None


def precheck(user_id: str, data: dict) -> dict:
    tz = _zone(data["schedule"].get("timezone") or "UTC")
    recipients = sorted({e.strip().lower() for e in data["recipients"] if e and "@" in e})
    recs: list[dict] = []

    # 1. Spam risk for each version (only while the spam detector is part of this app)
    ab = data.get("ab_test") or {}
    versions = {}
    if settings.SPAM_DETECTOR_ENABLED:
        versions["A"] = placement_model.score(data["subject"], data["body"], data["content_type"], user_id)
        if ab.get("enabled") and ab.get("subject_b") and ab.get("body_b"):
            versions["B"] = placement_model.score(ab["subject_b"], ab["body_b"], data["content_type"], user_id)
        worst = max(versions.values(), key=lambda v: v["score"])
        names = " and ".join(f"version {k}" for k in versions) if len(versions) > 1 else "your email"
        if worst["level"] == "poor":
            top = ", ".join(f"\"{k['matched']}\"" for k in worst["keywords"][:3]) or worst["issues"][0]["title"].lower()
            _rec(recs, "spam_high", "danger", f"High spam risk ({worst['score']}/100)",
                 f"{names.capitalize()} contains wording that spam filters flag, e.g. {top}. Replace the highlighted words before sending.")
        elif worst["level"] == "fair":
            _rec(recs, "spam_medium", "warning", f"Some spam risk ({worst['score']}/100)",
                 "A few words or formatting choices could push this into spam. Check the highlighted items in the spam check panel.")
        else:
            _rec(recs, "spam_low", "good", f"Low spam risk ({worst['score']}/100)", "The wording and formatting look clean.")

    # 2. Consumer behaviour from the user's own history
    history = _history(user_id)
    recipient_set = set(recipients)
    bad = sorted({r["recipient"].lower() for r in history if r["recipient"].lower() in recipient_set and r["last_event"] in ("bounced", "complained")})
    if bad:
        _rec(recs, "bounced", "danger", f"{len(bad)} contact{'s' if len(bad) != 1 else ''} bounced or marked you as spam before",
             "Emailing them again damages your sender reputation, so more of your emails land in spam.",
             {"type": "remove_recipients", "emails": bad, "label": f"Remove {len(bad)} contact{'s' if len(bad) != 1 else ''}"})

    cutoff = datetime.now(timezone.utc) - timedelta(hours=FATIGUE_HOURS)
    recent = sorted({
        r["recipient"].lower() for r in history
        if r["recipient"].lower() in recipient_set and r["status"] == "sent" and datetime.fromisoformat(r["sent_at"]) > cutoff
    } - set(bad))
    if recent:
        _rec(recs, "fatigue", "warning", f"{len(recent)} contact{'s' if len(recent) != 1 else ''} already heard from you in the last 3 days",
             "Emailing the same people too often lowers open rates and raises unsubscribes and spam reports.",
             {"type": "remove_recipients", "emails": recent, "label": f"Skip these {len(recent)}"})

    opened_ever = {r["recipient"].lower() for r in history if r["last_event"] in ("opened", "clicked")}
    if len(opened_ever) >= MIN_OPENS_FOR_LEARNING:
        received = defaultdict(int)
        for r in history:
            if r["status"] == "sent":
                received[r["recipient"].lower()] += 1
        cold = sorted(e for e in recipient_set if received[e] >= 3 and e not in opened_ever and e not in bad)
        if cold:
            _rec(recs, "unengaged", "tip", f"{len(cold)} contact{'s' if len(cold) != 1 else ''} never opened your last 3+ emails",
                 "Sending to people who never engage tells Gmail your emails aren't wanted. Consider a separate re-engagement email.",
                 {"type": "remove_recipients", "emails": cold, "label": f"Remove {len(cold)} inactive"})

    role = sorted(e for e in recipients if e.split("@")[0] in ROLE_BASED_PREFIXES)
    if role:
        _rec(recs, "role_based", "warning", f"{len(role)} generic address{'es' if len(role) != 1 else ''} (info@, office@…)",
             "Shared inboxes are often filtered and rarely reach a decision maker. Where possible, email the general manager or project lead directly.",
             {"type": "remove_recipients", "emails": role, "label": "Remove generic addresses"})

    # 3. Sending limits and domain warm-up
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = sum(1 for r in history if r["status"] == "sent" and datetime.fromisoformat(r["sent_at"]) >= today)
    remaining = max(settings.DAILY_SEND_LIMIT - sent_today, 0)
    if data["schedule"]["type"] == "now" and len(recipients) > remaining:
        _rec(recs, "daily_limit", "danger", "Over today's sending limit",
             f"Your plan allows {settings.DAILY_SEND_LIMIT} emails a day and {sent_today} were already sent today, "
             f"so only {remaining} of {len(recipients)} would go out. Schedule it for tomorrow or split the list.")
    total_sent = sum(1 for r in history if r["status"] == "sent")
    if total_sent < WARMUP_TOTAL and len(recipients) > WARMUP_BATCH:
        _rec(recs, "warmup", "warning", "Your sending domain is still new",
             f"For the first weeks, send to {WARMUP_BATCH} people or fewer per day and grow gradually. "
             "Large first sends from a new domain are a common reason for landing in spam.")

    # 4. Timing: each recipient's best window in their own local time (send_time: their clicks and replies,
    #    then their persona's, then a persona default)
    planned = _planned_time(data["schedule"], tz)
    learned = False
    if planned and recipients:
        timing = send_time.precheck_timing(user_id, recipients, planned.astimezone(timezone.utc),
                                           default_tz=send_time.DEFAULT_TIMEZONE)
        learned = timing["learned"]
        local = planned.astimezone(_zone(timing["main_timezone"]))
        zone_name = timing["main_timezone"].split("/")[-1].replace("_", " ")
        when = local.strftime("%A at %I:%M %p").replace(" 0", " ") + f" {zone_name} time"
        reach = f"{timing['in_window']} of {timing['total']} recipient{'s' if timing['total'] != 1 else ''} get it inside their best window."
        if timing["good"]:
            _rec(recs, "timing_good", "good", "Good timing", f"Sending {when}. {reach} {timing['reason']}")
        else:
            off_hours = local.weekday() >= 5 or local.hour < 7 or local.hour >= 21
            suggestion = timing["suggestion"].astimezone(tz)
            _rec(recs, "timing", "warning" if off_hours else "tip",
                 f"{'Weekend or out-of-hours send' if off_hours else 'Not the best time'}: {when}",
                 f"{reach} Best: {timing['best_label']}. {timing['reason']}",
                 None if data["schedule"]["type"] == "recurring" else
                 {"type": "schedule_at", "at": suggestion.isoformat(), "label": f"Schedule for {suggestion.strftime('%a %d %b, %I:%M %p').replace(' 0', ' ')}"})

    # 5. Content and sender best practices
    if versions and not any(v["stats"]["personalised"] for v in versions.values()):
        _rec(recs, "personalise", "tip", "Not personalized",
             "Emails that greet people by name (e.g. \"Hi {{first_name}}\") get noticeably more opens and replies from business contacts.")
    if not (ab.get("enabled")) and len(recipients) >= 50:
        _rec(recs, "ab_suggest", "tip", "Try testing two subject lines",
             f"With {len(recipients)} recipients, an A/B test would show which subject your contacts prefer.")
    if not data.get("reply_to"):
        _rec(recs, "reply_to", "tip", "No reply address set",
             "Add a real inbox under Sender details → Replies go to. Prospect replies are a strong trust signal for Gmail and Outlook.")

    order = {"danger": 0, "warning": 1, "tip": 2, "good": 3}
    recs.sort(key=lambda r: order[r["level"]])
    verdict = "risky" if any(r["level"] == "danger" for r in recs) else "review" if any(r["level"] == "warning" for r in recs) else "ready"
    return {
        "verdict": verdict,
        "recipient_count": len(recipients),
        "spam": versions,
        "recommendations": recs,
        "learned_from_history": bool(learned),
    }
