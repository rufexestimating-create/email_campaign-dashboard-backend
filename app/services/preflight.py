"""Campaign preflight (M7): eight pass / warn / fail checks run before a campaign is sent.

  audience_health      bounced or complained contacts, generic inboxes, contacts who never engage
  send_time            each recipient's best window in their local time (send_time.py)
  frequency_fatigue    contacts emailed in the last 72 hours, or 3+ times in a week
  gmail_domain_risk    Gmail's bulk-sender rules: complaint rate, unsubscribe, a new domain sending in bulk
  volume_spike         this send compared with the daily limit and your usual daily volume
  predicted_placement  estimated inbox / spam per mailbox provider (placement model + your placement labels)
  authentication       SPF, DKIM and DMARC DNS records of the sending domain
  content_fit          spam score and CAN-SPAM content (unsubscribe line, postal address)

Each check carries a message and, where one exists, a one-click action:
  remove_recipients {emails}, schedule_at {at}, apply_fix {version, fix}
Every placement number is an estimate; nothing here promises inbox delivery.
"""
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

import httpx

from app.core.config import settings
from app.services import campaign_advisor, placement_model, send_time, spam_checker, warmup
from app.services.event_tracking import provider_for
from app.services.spam_knowledge import ROLE_BASED_PREFIXES

FATIGUE_HOURS = 72
WEEKLY_CAP = 3
ENGAGED_EVENTS = ("clicked", "replied")
MIN_ENGAGED_FOR_LEARNING = 20  # below this, click tracking probably isn't set up, so "never engaged" means nothing
GMAIL_COMPLAINT_FAIL = 0.003   # Gmail's bulk-sender limit: keep spam complaints under 0.3%
GMAIL_COMPLAINT_WARN = 0.001   # ...and ideally under 0.1%
COMPLAINT_WINDOW_DAYS = 30
SPIKE_FACTOR = 3
MIN_PROVIDER_LABELS = 10
PLACEMENT_WARN = 0.2
PLACEMENT_FAIL = 0.45
DNS_URL = "https://dns.google/resolve"
DNS_TIMEOUT = 4.0
DNS_CACHE_SECONDS = 600
PROVIDER_NAMES = {"gmail": "Gmail", "outlook": "Outlook", "yahoo": "Yahoo", "icloud": "iCloud", "other": "Company / other"}

_dns_cache: dict[str, tuple[float, dict]] = {}


def _check(id_: str, name: str, status: str, message: str, detail: str = "", action: dict | None = None, data: dict | None = None) -> dict:
    return {"id": id_, "name": name, "status": status, "message": message, "detail": detail, "action": action, "data": data or {}}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _skip_label(n: int) -> str:
    return "Skip this contact" if n == 1 else f"Skip these {n} contacts"


def _remove(emails: list[str], label: str) -> dict:
    return {"type": "remove_recipients", "emails": emails, "label": label}


def _sent(history: list[dict]) -> list[dict]:
    return [r for r in history if r["status"] == "sent"]


def _when(row: dict) -> datetime:
    return datetime.fromisoformat(row["sent_at"])


# ---------- 1. Audience health ----------

def audience_health(recipients: list[str], history: list[dict]) -> dict:
    wanted = set(recipients)
    bad = sorted({r["recipient"].lower() for r in history if r["recipient"].lower() in wanted and r["last_event"] in ("bounced", "complained")})
    role = sorted(e for e in recipients if e.split("@")[0] in ROLE_BASED_PREFIXES and e not in bad)
    engaged = {r["recipient"].lower() for r in history if r["last_event"] in ENGAGED_EVENTS}
    cold: list[str] = []
    if len(engaged) >= MIN_ENGAGED_FOR_LEARNING:
        received = Counter(r["recipient"].lower() for r in _sent(history))
        cold = sorted(e for e in wanted if received[e] >= 3 and e not in engaged and e not in bad)
    data = {"bounced_or_complained": len(bad), "generic_addresses": len(role), "never_engaged": len(cold), "engagement_tracked": len(engaged) >= MIN_ENGAGED_FOR_LEARNING}
    if bad:
        return _check("audience_health", "Audience health", "fail", f"{_plural(len(bad), 'contact')} bounced or marked you as spam before",
                      "Emailing them again damages your sender reputation, so more of your emails land in spam.",
                      _remove(bad, f"Remove {_plural(len(bad), 'contact')}"), data)
    if role or cold:
        parts = ([f"{_plural(len(role), 'generic address', 'generic addresses')} (info@, office@…)"] if role else []) + \
                ([f"{_plural(len(cold), 'contact')} who never clicked or replied to their last 3+ emails"] if cold else [])
        return _check("audience_health", "Audience health", "warn", " and ".join(parts).capitalize(),
                      "Shared inboxes rarely reach a decision maker, and mailing people who never engage tells Gmail your email isn't wanted.",
                      _remove(role + cold, f"Remove {_plural(len(role) + len(cold), 'contact')}"), data)
    return _check("audience_health", "Audience health", "pass", "No bounced, complaining or generic addresses",
                  "" if data["engagement_tracked"] else "Contacts who never engage are checked once click and reply tracking has collected data.", data=data)


# ---------- 2. Send time ----------

def send_time_check(timing: dict | None, campaign_tz, recurring: bool) -> dict:
    if timing is None:
        return _check("send_time", "Send time", "pass", "No send time to check yet", "Pick when to send and the time is checked against each recipient's best window.")
    zone = timing["main_timezone"]
    data = {k: timing[k] for k in ("in_window", "total", "learned", "best_label")} | {"sources": timing.get("sources", {})}
    reach = f"{timing['in_window']} of {_plural(timing['total'], 'recipient')} {'gets' if timing['total'] == 1 else 'get'} it inside their best window"
    if timing["good"]:
        return _check("send_time", "Send time", "pass", reach.capitalize(), timing["reason"], data=data)
    suggestion = timing["suggestion"].astimezone(campaign_tz)
    action = None if recurring else {"type": "schedule_at", "at": suggestion.isoformat(),
                                     "label": f"Schedule for {suggestion.strftime('%a %d %b, %I:%M %p').replace(' 0', ' ')}"}
    return _check("send_time", "Send time", "warn", f"Only {reach}", f"Best: {timing['best_label']}. {timing['reason']}", action, data | {"zone": zone})


# ---------- 3. Frequency fatigue ----------

def frequency_fatigue(recipients: list[str], history: list[dict], now: datetime) -> dict:
    wanted = set(recipients)
    recent, week = set(), Counter()
    for r in _sent(history):
        e = r["recipient"].lower()
        if e not in wanted:
            continue
        age = now - _when(r)
        if age <= timedelta(hours=FATIGUE_HOURS):
            recent.add(e)
        if age <= timedelta(days=7):
            week[e] += 1
    over_cap = sorted(e for e, n in week.items() if n >= WEEKLY_CAP)
    data = {"emailed_last_72h": len(recent), "at_weekly_cap": len(over_cap)}
    if over_cap:
        return _check("frequency_fatigue", "Frequency fatigue", "fail", f"{_plural(len(over_cap), 'contact')} already got {WEEKLY_CAP}+ emails this week",
                      "Mailing the same people this often drives unsubscribes and spam reports.", _remove(over_cap, _skip_label(len(over_cap))), data)
    if recent:
        skip = sorted(recent)
        return _check("frequency_fatigue", "Frequency fatigue", "warn", f"{_plural(len(skip), 'contact')} heard from you in the last 3 days",
                      "Leave a few days between emails to the same person.", _remove(skip, _skip_label(len(skip))), data)
    return _check("frequency_fatigue", "Frequency fatigue", "pass", "Nobody on this list was emailed in the last 3 days", data=data)


# ---------- 4. Gmail domain risk ----------

def gmail_domain_risk(recipients: list[str], history: list[dict], has_unsubscribe: bool, now: datetime) -> dict:
    gmail = [e for e in recipients if provider_for(e) == "gmail"]
    since = now - timedelta(days=COMPLAINT_WINDOW_DAYS)
    window = [r for r in _sent(history) if _when(r) >= since]
    complaints = sum(r["last_event"] == "complained" for r in window)
    rate = complaints / len(window) if window else 0.0
    new_domain = len(_sent(history)) < campaign_advisor.WARMUP_TOTAL
    data = {"gmail_recipients": len(gmail), "complaint_rate": round(rate, 4), "sends_30_days": len(window), "complaints_30_days": complaints}
    if not gmail:
        return _check("gmail_domain_risk", "Gmail domain risk", "pass", "No Gmail addresses on this list", data=data)
    if rate >= GMAIL_COMPLAINT_FAIL:
        return _check("gmail_domain_risk", "Gmail domain risk", "fail", f"Spam complaint rate is {rate:.2%} (Gmail's limit is 0.3%)",
                      f"Over the limit, Gmail starts sending your email to spam. Remove contacts who complained and only mail people who expect you. "
                      f"{complaints} complaint{'s' if complaints != 1 else ''} in {len(window)} sends over 30 days.", data=data)
    issues = []
    if rate >= GMAIL_COMPLAINT_WARN:
        issues.append(f"complaint rate is {rate:.2%} (aim for under 0.1%)")
    if not has_unsubscribe:
        issues.append("there's no unsubscribe line, which Gmail requires from bulk senders")
    if new_domain and len(gmail) > campaign_advisor.WARMUP_BATCH:
        issues.append(f"{len(gmail)} Gmail addresses from a new domain (start with {campaign_advisor.WARMUP_BATCH} or fewer a day)")
    if issues:
        return _check("gmail_domain_risk", "Gmail domain risk", "warn", f"{_plural(len(gmail), 'Gmail address', 'Gmail addresses')}: " + "; ".join(issues),
                      "Gmail asks bulk senders for SPF, DKIM and DMARC, an easy unsubscribe and a complaint rate under 0.3%.", data=data)
    return _check("gmail_domain_risk", "Gmail domain risk", "pass",
                  f"{_plural(len(gmail), 'Gmail address', 'Gmail addresses')}, complaint rate {rate:.2%}", "Within Gmail's bulk-sender rules.", data=data)


# ---------- 5. Volume spike ----------

def volume_spike(count: int, history: list[dict], now: datetime, sending_now: bool,
                 daily_limit: int | None = None, warm_week: int | None = None) -> dict:
    """`daily_limit` is today's warm-up limit (warmup.daily_limit); `warm_week` is set while the domain is warming up."""
    daily_limit = daily_limit or settings.DAILY_SEND_LIMIT
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent = _sent(history)
    sent_today = sum(_when(r) >= today for r in sent)
    per_day = Counter(_when(r).date() for r in sent if _when(r) >= now - timedelta(days=30) and _when(r) < today)
    typical = median(per_day.values()) if per_day else 0
    remaining = max(daily_limit - sent_today, 0)
    data = {"recipients": count, "sent_today": sent_today, "daily_limit": daily_limit, "typical_daily": typical, "warm_up_week": warm_week}
    if sending_now and count > remaining:
        why = (f"Your domain is in week {warm_week} of its warm-up, so today's limit is {daily_limit} emails"
               if warm_week else f"Your plan allows {daily_limit} emails a day")
        return _check("volume_spike", "Volume spike", "fail", f"Over today's sending limit: {_plural(count, 'recipient')}, {remaining} sends left today",
                      f"{why} and {sent_today} went out today. Schedule the rest for tomorrow or split the list.", data=data)
    limit = max(SPIKE_FACTOR * typical, campaign_advisor.WARMUP_BATCH)
    if count > limit:
        basis = f"about {round(typical)} a day" if typical else "no regular sending yet"
        return _check("volume_spike", "Volume spike", "warn", f"{_plural(count, 'recipient')} is a big jump from your usual volume ({basis})",
                      "Sudden jumps in volume look like a compromised or spamming account. Grow sending gradually, or split this list over a few days.", data=data)
    return _check("volume_spike", "Volume spike", "pass", f"{_plural(count, 'recipient')} is in line with your usual sending", data=data)


# ---------- 6. Predicted placement per provider ----------

def predicted_placement(recipients: list[str], spam_probability: float, label_rows: list[dict], estimate_note: str) -> dict:
    by_provider = Counter(provider_for(e) for e in recipients)
    observed: dict[str, list[int]] = defaultdict(list)
    for row in label_rows:
        if row["placement"] in ("spam", "complained"):
            observed[row["provider"]].append(1)
        elif row["placement"] in ("inbox", "promotions"):
            observed[row["provider"]].append(0)
    providers = []
    for provider, n in by_provider.most_common():
        labels = observed.get(provider, [])
        if len(labels) >= MIN_PROVIDER_LABELS:
            p, basis = 0.5 * spam_probability + 0.5 * (sum(labels) / len(labels)), "model + your placement labels"
        else:
            p, basis = spam_probability, "model"
        providers.append({"provider": provider, "name": PROVIDER_NAMES[provider], "recipients": n, "spam_probability": round(p, 3),
                          "inbox_probability": round(1 - p, 3), "basis": basis, "labels": len(labels)})
    worst = max((p["spam_probability"] for p in providers), default=0.0)
    status = "fail" if worst >= PLACEMENT_FAIL else "warn" if worst >= PLACEMENT_WARN else "pass"
    summary = ", ".join(f"{p['name']} {round(p['inbox_probability'] * 100)}%" for p in providers) or "no recipients"
    return _check("predicted_placement", "Predicted placement", status, f"Estimated inbox placement: {summary}", estimate_note,
                  data={"providers": providers, "estimated": True})


# ---------- 7. SPF / DKIM / DMARC ----------

def sender_domain(from_email: str | None) -> str:
    m = re.search(r"@([A-Za-z0-9.-]+)", from_email or settings.DEFAULT_FROM_EMAIL)
    return m.group(1).lower().rstrip(".") if m else ""


def dns_txt(name: str) -> list[str]:
    """TXT records over DNS-over-HTTPS (no extra DNS library needed). Raises on network errors."""
    res = httpx.get(DNS_URL, params={"name": name, "type": "TXT"}, timeout=DNS_TIMEOUT)
    res.raise_for_status()
    return [a["data"].replace('" "', "").strip('"') for a in res.json().get("Answer", []) if a.get("type") == 16]


def lookup_auth(domain: str, resolver=None) -> dict:
    """SPF on the domain or Resend's send.<domain>, DKIM at resend._domainkey.<domain>, DMARC at _dmarc.<domain>."""
    cached = _dns_cache.get(domain)
    if cached and time.time() - cached[0] < DNS_CACHE_SECONDS:
        return cached[1]
    resolver = resolver or dns_txt
    spf = [t for name in (domain, f"send.{domain}") for t in resolver(name) if t.lower().startswith("v=spf1")]
    dkim = [t for t in resolver(f"resend._domainkey.{domain}") if "p=" in t]
    dmarc = [t for t in resolver(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    policy = re.search(r"\bp=(\w+)", dmarc[0], re.I).group(1).lower() if dmarc and re.search(r"\bp=(\w+)", dmarc[0], re.I) else None
    result = {"spf": bool(spf), "dkim": bool(dkim), "dmarc": bool(dmarc), "dmarc_policy": policy}
    _dns_cache[domain] = (time.time(), result)
    return result


def authentication(domain: str, resolver=None) -> dict:
    if not domain:
        return _check("authentication", "SPF / DKIM / DMARC", "warn", "No sender address to check")
    if domain == "resend.dev":
        return _check("authentication", "SPF / DKIM / DMARC", "warn", "Sending from Resend's test domain (resend.dev)",
                      "Test-domain emails only reach your own address. Send from your verified domain (arvistry.com) for real campaigns.", data={"domain": domain})
    try:
        auth = lookup_auth(domain, resolver)
    except Exception:
        return _check("authentication", "SPF / DKIM / DMARC", "warn", f"Couldn't look up DNS records for {domain}",
                      "Check your connection and try again. The other checks still ran.", data={"domain": domain})
    data = {"domain": domain, **auth}
    missing = [n for n in ("spf", "dkim") if not auth[n]]
    if missing:
        return _check("authentication", "SPF / DKIM / DMARC", "fail", f"{' and '.join(m.upper() for m in missing)} missing for {domain}",
                      "Without them, Gmail and Outlook can't confirm the email is really from you. Add the records Resend shows under Domains, in your DNS (Hostinger).", data=data)
    if not auth["dmarc"]:
        return _check("authentication", "SPF / DKIM / DMARC", "warn", f"SPF and DKIM pass, but {domain} has no DMARC record",
                      "Gmail and Yahoo require DMARC from bulk senders. Add a TXT record at _dmarc." + domain + ' such as "v=DMARC1; p=none; rua=mailto:you@' + domain + '".', data=data)
    note = " (monitoring only; move to quarantine once reports look clean)" if auth["dmarc_policy"] == "none" else ""
    return _check("authentication", "SPF / DKIM / DMARC", "pass", f"SPF, DKIM and DMARC are set up for {domain}", f"DMARC policy: {auth['dmarc_policy'] or 'unknown'}{note}.", data=data)


# ---------- 8. Content fit ----------

def content_fit(versions: dict[str, dict]) -> dict:
    worst_version, worst = max(versions.items(), key=lambda kv: kv[1]["score"])
    missing = []
    fix_action = None
    for v, result in versions.items():
        issues = {i["id"]: i for i in result["issues"]}
        if "no_unsubscribe" in issues:
            missing.append(f"unsubscribe line{f' (version {v})' if len(versions) > 1 else ''}")
            fix_action = fix_action or {"type": "apply_fix", "version": v, "fix": issues["no_unsubscribe"]["fix"], "label": "Add an unsubscribe line"}
        if "no_address" in issues:
            missing.append(f"postal address{f' (version {v})' if len(versions) > 1 else ''}")
    data = {"scores": {v: r["score"] for v, r in versions.items()}, "missing": missing}
    label = f"version {worst_version}" if len(versions) > 1 else "the email"
    top = ", ".join(r["title"] for r in worst["reasons"][:3])
    if worst["level"] == "poor":
        return _check("content_fit", "Content fit", "fail", f"High spam risk in {label} ({worst['score']}/100)", f"Biggest problems: {top}.", fix_action, data)
    if worst["level"] == "fair" or missing:
        message = f"Some spam risk in {label} ({worst['score']}/100)" if worst["level"] == "fair" else f"Missing {', '.join(missing)}"
        detail = (f"Biggest problems: {top}. " if worst["level"] == "fair" and top else "") + \
                 ("US law (CAN-SPAM) requires an unsubscribe option and a postal address in marketing email." if missing else "")
        return _check("content_fit", "Content fit", "warn", message, detail.strip(), fix_action, data)
    return _check("content_fit", "Content fit", "pass", f"Content looks clean ({worst['score']}/100 spam risk)", data=data)


# ---------- M8: what was done with the advice ----------

MIN_OVERRIDE_REASON = 5


class OverrideReasonRequired(ValueError):
    pass


def decision_row(user_id: str, d: dict) -> dict:
    """A failed check still failing when the campaign goes out is an override, and needs a reason."""
    checks = [{"id": c["id"], "status": c["status"], "message": c["message"]} for c in d["checks"]]
    accepted = list(dict.fromkeys(d["accepted"]))
    proceeding = d["outcome"] != "cancelled"
    overridden = proceeding and any(c["status"] == "fail" for c in checks)
    reason = (d.get("override_reason") or "").strip()
    if overridden and len(reason) < MIN_OVERRIDE_REASON:
        raise OverrideReasonRequired("A check failed. Give a short reason for sending anyway.")
    return {
        "user_id": user_id, "campaign_id": d.get("campaign_id"), "verdict": d["verdict"], "checks": checks, "accepted": accepted,
        "ignored": [c["id"] for c in checks if c["status"] in ("warn", "fail") and c["id"] not in accepted],
        "overridden": overridden, "override_reason": reason if overridden else None, "outcome": d["outcome"],
    }


def record_decision(user_id: str, d: dict) -> dict:
    from app.db.supabase import get_supabase

    row = decision_row(user_id, d)
    db = get_supabase()
    if row["campaign_id"] and not db.table("campaigns").select("id").eq("id", row["campaign_id"]).eq("user_id", user_id).limit(1).execute().data:
        row["campaign_id"] = None  # not this user's campaign: keep the decision, drop the link
    return db.table("preflight_decisions").insert(row).execute().data[0]


def decision_summary(rows: list[dict]) -> dict:
    """Per check: how often its advice was taken vs ignored, plus how often a failed send was overridden."""
    per_check: dict[str, dict] = defaultdict(lambda: {"accepted": 0, "ignored": 0})
    for r in rows:
        for cid in r["accepted"]:
            per_check[cid]["accepted"] += 1
        for cid in r["ignored"]:
            per_check[cid]["ignored"] += 1
    return {
        "decisions": len(rows),
        "overridden": sum(r["overridden"] for r in rows),
        "cancelled": sum(r["outcome"] == "cancelled" for r in rows),
        "per_check": dict(per_check),
    }


def list_decisions(user_id: str, limit: int = 100) -> dict:
    from app.db.supabase import get_supabase

    rows = get_supabase().table("preflight_decisions").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute().data
    return {"summary": decision_summary(rows), "decisions": rows}


# ---------- All together ----------

def load_placement_labels(user_id: str) -> list[dict]:
    from app.db.supabase import get_supabase

    since = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    return get_supabase().table("placement_labels").select("provider, placement").eq("user_id", user_id).gte("observed_at", since).limit(5000).execute().data


def preflight(user_id: str, data: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    campaign_tz = campaign_advisor._zone(data["schedule"].get("timezone") or "UTC")
    recipients = sorted({e.strip().lower() for e in data["recipients"] if e and "@" in e})

    ab = data.get("ab_test") or {}
    contents = [(data["subject"], data["body"])]
    if ab.get("enabled") and ab.get("subject_b") and ab.get("body_b"):
        contents.append((ab["subject_b"], ab["body_b"]))
    spam_on = settings.SPAM_DETECTOR_ENABLED
    versions = {k: placement_model.score(s, b, data["content_type"], user_id) for k, (s, b) in zip("AB", contents)} if spam_on else {}
    # Gmail's bulk-sender rules need an unsubscribe line whether or not the spam detector is on
    has_unsubscribe = all(
        (versions[k]["stats"] if spam_on else spam_checker.analyze(s, b, data["content_type"])["stats"])["has_unsubscribe"]
        for k, (s, b) in zip("AB", contents)
    )

    history = campaign_advisor._history(user_id)
    planned = campaign_advisor._planned_time(data["schedule"], campaign_tz)
    timing = None
    if planned and recipients:
        # Contacts with no ZIP / state are assumed to be on US Eastern time, never the sender's own timezone
        # (the team may be working from another country while every contact is in the US)
        timing = send_time.precheck_timing(user_id, recipients, planned.astimezone(timezone.utc), default_tz=send_time.DEFAULT_TIMEZONE)

    checks = [
        audience_health(recipients, history),
        send_time_check(timing, campaign_tz, data["schedule"]["type"] == "recurring"),
        frequency_fatigue(recipients, history, now),
        gmail_domain_risk(recipients, history, has_unsubscribe, now),
        volume_spike(len(recipients), history, now, data["schedule"]["type"] == "now", *warmup.daily_limit(user_id)),
        authentication(sender_domain(data.get("from_email"))),
    ]
    if spam_on:  # the two checks built on the spam score
        worst = max(versions.values(), key=lambda v: v["score"])
        checks.insert(5, predicted_placement(recipients, worst["score"] / 100, load_placement_labels(user_id), worst["estimate_note"]))
        checks.append(content_fit(versions))
    counts = Counter(c["status"] for c in checks)
    return {
        "verdict": "fail" if counts["fail"] else "warn" if counts["warn"] else "pass",
        "counts": {s: counts[s] for s in ("pass", "warn", "fail")},
        "recipient_count": len(recipients),
        "checks": checks,
        "spam": versions,
        "note": "Placement figures are estimates. No tool can guarantee inbox delivery.",
    }
