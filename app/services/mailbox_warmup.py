"""Mailbox warm-up: the team's own mailboxes (e.g. Hostinger ali@, sara@) email each other and answer.

A new mailbox that suddenly emails clients lands in spam. One that has been sending and receiving ordinary mail for a
few weeks, mail that gets opened, answered and taken out of spam, doesn't. The scheduler does that here:

1. plan     (every few minutes): each active mailbox gets today's number of emails, which grows day by day (RAMP),
            each to another of the team's mailboxes at a random time during office hours.
2. send     (every tick): emails whose time has come go out over the mailbox's own SMTP. Each carries an
            X-Warmup-Id header so the receiving side can recognise it.
3. inboxes  (every few minutes): every mailbox is read over IMAP, Inbox and Spam. Each warm-up email found is recorded
            (where it landed), moved out of Spam if it went there, marked read, sometimes starred, and about half get
            a reply a little later, in the same thread.

Only the team's own mailboxes take part. Passwords are stored encrypted (MAILBOX_SECRET_KEY).
"""
import base64
import hashlib
import imaplib
import logging
import random
import re
import smtplib
import socket
import ssl
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.services import warmup_content

logger = logging.getLogger(__name__)

HEADER = "X-Warmup-Id"
# (last day of the step, emails per day). After the last step a mailbox sends its own daily_limit.
RAMP = [(3, 3), (7, 6), (14, 12), (21, 20)]
WINDOW = (settings.WARMUP_WINDOW_START, settings.WARMUP_WINDOW_END)  # sending hours, local time; may cross midnight
REPLY_RATE = 0.5                # share of received warm-up emails that get answered
REPLY_DELAY_MIN = (10, 120)     # minutes
STAR_RATE = 0.2
PLAN_SECONDS = 300
CHECK_SECONDS = 300
SEND_PER_TICK = 20
LOOKBACK_DAYS = 3
MAX_MAILBOXES = 20
TIMEOUT = 30
# A server that can't be reached (timeout, network blip) is tried again later; only after this many failures in a row
# is the mailbox set to 'error'. A wrong password stops it at once.
# Advanced settings (Instantly's names), per mailbox in mailboxes.warmup_settings; missing keys use these
ADVANCED_DEFAULTS = {
    "slow_ramp": True,        # off = the daily limit from day one (only for mailboxes that are already warm)
    "random_daily": False,    # send a random number each day (60-100% of the day's target)
    "open_rate": 100,         # % of received warm-up emails opened
    "spam_protection": 100,   # % of warm-up emails found in spam that are moved to the inbox
    "important_rate": 20,     # % of received warm-up emails flagged (starred / important)
    "read_emulation": True,   # download the whole email before marking it read, like a mail app does
    "timezone": None,         # IANA name, e.g. "America/New_York"; None = WARMUP_TIMEZONE
    "window_start": WINDOW[0],  # sending window, local hours: from ...
    "window_end": WINDOW[1],    # ... until (exclusive)
    "topic": warmup_content.DEFAULT_TOPIC,  # what the warm-up emails are about (warmup_content.TOPICS)
    "custom_subjects": [],    # the user's own subject lines, mixed in
    "custom_messages": [],    # the user's own messages, mixed in ({first_name} = the recipient)
    "min_gap_minutes": 0,     # at least this long between two of its warm-up emails (0 = no minimum)
    "reply_delay": "normal",  # how soon it answers a warm-up email: REPLY_DELAYS
}
REPLY_DELAYS = {"fast": (5, 30), "normal": REPLY_DELAY_MIN, "slow": (120, 360)}  # minutes after it arrives
CONTENT_KEYS = ("topic", "custom_subjects", "custom_messages")
HISTORY_DAYS = 30
READY_DAY = 14                  # earliest warm-up day a healthy mailbox is called ready for real campaigns
FAILS_BEFORE_ERROR = 3
RETRY_MINUTES = 10
MAX_SEND_TRIES = 3              # an email that fails to send is tried this many times in all

HOSTINGER = {"smtp_host": "smtp.hostinger.com", "smtp_port": 465, "imap_host": "imap.hostinger.com", "imap_port": 993}

_last = {"plan": 0.0, "check": 0.0}
_failures: dict[str, int] = {}  # mailbox id -> unreachable-server failures in a row


def _db():
    from app.db.supabase import get_supabase

    return get_supabase()


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.WARMUP_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


# ---------- Passwords ----------

def _fernet() -> Fernet:
    secret = settings.MAILBOX_SECRET_KEY or f"mailbox:{settings.JWT_SECRET_KEY}"
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(password: str) -> str:
    return _fernet().encrypt(password.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("The saved password can't be read (the server key changed). Enter the password again.")


# ---------- Pure: ramp and plan ----------

def warmup_day(started_on: date, today: date) -> int:
    return max(1, (today - started_on).days + 1)


def advanced(mb: dict) -> dict:
    """A mailbox's advanced warm-up settings, defaults filled in."""
    return {**ADVANCED_DEFAULTS, **{k: v for k, v in (mb.get("warmup_settings") or {}).items() if k in ADVANCED_DEFAULTS}}


def content_of(mb: dict) -> dict:
    """What a mailbox's warm-up emails are about: {topic, custom_subjects, custom_messages}."""
    adv = advanced(mb)
    return {k: adv[k] for k in CONTENT_KEYS}


def preview(user_id: str, content: dict, mailbox_id: str | None = None, count: int = 3) -> list[dict]:
    """Sample warm-up emails for content settings that may not be saved yet, written as this mailbox to another."""
    boxes = list_mailboxes(user_id)
    me = next((m for m in boxes if m["id"] == mailbox_id), boxes[0] if boxes else None)
    other = next((m for m in boxes if me and m["id"] != me["id"]), None)
    sender = warmup_content.first_name(me.get("display_name"), me["email"]) if me else "Ali"
    to = warmup_content.first_name(other.get("display_name"), other["email"]) if other else "Sara"
    rng = random.Random()
    out = []
    for _ in range(count):
        subject, body = warmup_content.compose_initial(rng, sender, to, content)
        out.append({"subject": subject, "body": body, "from": me["email"] if me else None, "to": other["email"] if other else None})
    return out


def clock(mb: dict, default_tz: ZoneInfo) -> tuple[ZoneInfo, int, int]:
    """A mailbox's timezone and sending window (start, end hour), falling back to the defaults when unset or invalid."""
    adv = advanced(mb)
    tz = default_tz
    if adv.get("timezone"):
        try:
            tz = ZoneInfo(adv["timezone"])
        except Exception:
            tz = default_tz
    start, end = adv.get("window_start"), adv.get("window_end")
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= 23 and 1 <= end <= 24 and start != end):
        start, end = WINDOW
    return tz, start, end


def send_offsets(rng: random.Random, count: int, span: float, gap_minutes: int = 0) -> list[float]:
    """`count` random moments (seconds from the start) within `span` seconds. With a gap, any two are at least
    `gap_minutes` apart; if they can't all fit, fewer are sent."""
    if not gap_minutes:
        return [rng.uniform(0, span) for _ in range(count)]
    gap = gap_minutes * 60
    count = min(count, int(span // gap) + 1)
    free = span - gap * (count - 1)
    return [x + i * gap for i, x in enumerate(sorted(rng.uniform(0, free) for _ in range(count)))]


def window_hours(start: int, end: int) -> int:
    """Length of a sending window; one whose end is before its start runs past midnight (16 -> 3 is 11 hours)."""
    return end - start if start < end else 24 - start + end


def window_bounds(now: datetime, tz: ZoneInfo, start: int, end: int) -> tuple[date, datetime, datetime]:
    """The sending window `now` belongs to: (its day, opens, closes). A window that crosses midnight belongs to the day
    it opened, so at 1 AM inside a 4 PM - 3 AM window the day is yesterday."""
    local = now.astimezone(tz)
    day = local.date()
    if start > end and local.hour < end:
        day -= timedelta(days=1)
    midnight = datetime.combine(day, datetime.min.time(), tz)
    opens = midnight + timedelta(hours=start)
    return day, opens, opens + timedelta(hours=window_hours(start, end))


def daily_target(day: int, limit: int, increase: int | None = None, slow_ramp: bool = True) -> int:
    """Emails a mailbox sends on its `day`th day of warm-up. With an `increase` (Instantly-style): 1 on day 1, then that
    many more each day up to its daily limit. Without: the standard ramp, 3 a day at first, rising to its daily limit.
    `slow_ramp` off: the daily limit straight away."""
    if not slow_ramp:
        return limit
    if increase:
        return min(limit, 1 + (day - 1) * increase)
    for last_day, per_day in RAMP:
        if day <= last_day:
            return min(per_day, limit)
    return limit


def plan_day(mailboxes: list[dict], already: dict[str, int], now: datetime, rng: random.Random, tz: ZoneInfo) -> list[dict]:
    """Today's warm-up emails for mailboxes that have none planned yet: [{from, to, at, plan_date}] (UTC times).

    Each mailbox plans in its own timezone and sending window (clock()); `plan_date` is its local date. `already` counts
    each mailbox's emails planned for its local today, so a mailbox added in the afternoon still gets its share and one
    that's planned isn't planned twice. Times fall in what's left of today's window.
    """
    if len(mailboxes) < 2:
        return []
    sends = []
    for mb in mailboxes:
        if already.get(mb["id"]):
            continue
        mtz, w_start, w_end = clock(mb, tz)
        today, opens, end = window_bounds(now, mtz, w_start, w_end)
        start = max(now.astimezone(mtz) + timedelta(minutes=2), opens)
        if start >= end:
            continue
        span = (end - start).total_seconds()
        if mb.get("warmup_weekdays_only") and today.weekday() >= 5:
            continue  # "Weekdays only": nothing on Saturday or Sunday
        adv = advanced(mb)
        target = daily_target(warmup_day(_as_date(mb["warmup_started_on"]), today), mb["daily_limit"], mb.get("warmup_increase"), adv["slow_ramp"])
        # The share of today still left, so a mailbox planned at 4 pm doesn't fit a whole day into two hours
        target = max(1, round(target * min(1.0, span / (window_hours(w_start, w_end) * 3600))))
        if adv["random_daily"] and target > 1:
            target = rng.randint(max(1, -(-target * 6 // 10)), target)
        others = [m for m in mailboxes if m["id"] != mb["id"]]
        gap = adv.get("min_gap_minutes") or 0
        offsets = send_offsets(rng, target, span, gap) if gap else None  # without a gap: as before, one by one
        for i in range(len(offsets) if offsets is not None else target):
            at = start + timedelta(seconds=offsets[i] if offsets is not None else rng.uniform(0, span))
            sends.append({"from": mb, "to": rng.choice(others), "at": at.astimezone(timezone.utc), "plan_date": today})
    return sorted(sends, key=lambda s: s["at"])


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


# ---------- Pure: report ----------

def summarize(mailboxes: list[dict], messages: list[dict], today: date, tz: ZoneInfo, days: int = 14) -> list[dict]:
    """Per mailbox: warm-up day, today's progress, and where its emails landed (last 7 days, plus a daily series)."""
    stats = {
        m["id"]: {
            "sent_today": 0, "initial_today": 0, "received_today": 0, "scheduled_today": 0,
            "inbox_7d": 0, "spam_7d": 0, "rescued_7d": 0, "received_7d": 0, "failed_7d": 0, "last_failure": None, "last_activity_at": None, "replies_7d": 0, "waiting": 0,
            "series": {(today - timedelta(days=i)).isoformat(): {"sent": 0, "inbox": 0, "spam": 0} for i in range(days - 1, -1, -1)},
        }
        for m in mailboxes
    }
    week_ago = today - timedelta(days=6)
    for msg in messages:
        sender, receiver = stats.get(msg["from_mailbox_id"]), stats.get(msg["to_mailbox_id"])
        sent_day = _local_date(msg.get("sent_at"), tz)
        found_day = _local_date(msg.get("found_at"), tz)
        if sender and msg["status"] == "failed":
            failed_day = _local_date(msg.get("scheduled_at"), tz)
            if failed_day and failed_day >= week_ago:
                sender["failed_7d"] += 1
                if msg.get("error") and (not sender["last_failure"] or msg["scheduled_at"] > sender["last_failure"]["at"]):
                    sender["last_failure"] = {"at": msg["scheduled_at"], "error": msg["error"]}
        if sender and msg.get("sent_at") and (not sender["last_activity_at"] or msg["sent_at"] > sender["last_activity_at"]):
            sender["last_activity_at"] = msg["sent_at"]
        if receiver and msg.get("found_at") and (not receiver["last_activity_at"] or msg["found_at"] > receiver["last_activity_at"]):
            receiver["last_activity_at"] = msg["found_at"]
        if sender:
            if msg["status"] == "scheduled" and _as_date(msg["plan_date"]) == today:
                sender["scheduled_today"] += 1
            if msg["status"] == "sent":
                if sent_day == today:
                    sender["sent_today"] += 1
                    if msg["kind"] == "initial":
                        sender["initial_today"] += 1  # what counts against the day's target; replies come on top
                if sent_day and sent_day.isoformat() in sender["series"]:
                    sender["series"][sent_day.isoformat()]["sent"] += 1
                if msg.get("landed_in") and sent_day and sent_day.isoformat() in sender["series"]:
                    sender["series"][sent_day.isoformat()][msg["landed_in"]] += 1
                if sent_day and sent_day >= week_ago:
                    if msg.get("landed_in") == "inbox":
                        sender["inbox_7d"] += 1
                    elif msg.get("landed_in") == "spam":
                        sender["spam_7d"] += 1
                    if msg["kind"] == "reply":
                        sender["replies_7d"] += 1
                if not msg.get("landed_in"):
                    sender["waiting"] += 1
        if receiver and found_day:
            if found_day == today:
                receiver["received_today"] += 1
            if found_day >= week_ago:
                receiver["received_7d"] += 1
            if msg.get("rescued") and found_day >= week_ago:
                receiver["rescued_7d"] += 1

    out = []
    for mb in mailboxes:
        s = stats[mb["id"]]
        day = warmup_day(_as_date(mb["warmup_started_on"]), today)
        landed = s["inbox_7d"] + s["spam_7d"]
        inbox_rate = round(s["inbox_7d"] / landed, 3) if landed else None
        out.append({
            "id": mb["id"],
            "email": mb["email"],
            "display_name": mb.get("display_name"),
            "status": mb["status"],
            "last_error": mb.get("last_error"),
            "last_checked_at": mb.get("last_checked_at"),
            "daily_limit": mb["daily_limit"],
            "warmup_day": day,
            "target_today": daily_target(day, mb["daily_limit"], mb.get("warmup_increase"), advanced(mb)["slow_ramp"]),
            "warmup_started_on": str(mb["warmup_started_on"]),
            "warmup_settings": advanced(mb),
            "sent_7d": sum(v["sent"] for v in list(s["series"].values())[-7:]),
            "warmup_increase": mb.get("warmup_increase"),
            "warmup_reply_rate": mb.get("warmup_reply_rate"),
            "sent_today": s["sent_today"],
            "initial_today": s["initial_today"],
            "scheduled_today": s["scheduled_today"],
            "received_today": s["received_today"],
            "inbox_7d": s["inbox_7d"],
            "spam_7d": s["spam_7d"],
            "inbox_rate_7d": inbox_rate,
            "rescued_7d": s["rescued_7d"],
            "received_7d": s["received_7d"],
            "failed_7d": s["failed_7d"],
            "last_failure": s["last_failure"],
            "last_activity_at": s["last_activity_at"],
            "warmup_weekdays_only": bool(mb.get("warmup_weekdays_only")),
            "replies_7d": s["replies_7d"],
            "waiting": s["waiting"],
            "health": _health(mb["status"], inbox_rate, landed),
            "ready": mb["status"] == "active" and day >= READY_DAY and _health(mb["status"], inbox_rate, landed) == "healthy",
            "series": [{"date": d, **v} for d, v in s["series"].items()],
            "smtp_host": mb.get("smtp_host"),
            "imap_host": mb.get("imap_host"),
        })
    return out


def reply_rate(mb: dict) -> float:
    """Share of the warm-up emails this mailbox receives that it answers: its own setting, else the standard."""
    rate = mb.get("warmup_reply_rate")
    return REPLY_RATE if rate is None else rate / 100


def _health(status: str, inbox_rate: float | None, landed: int) -> str:
    if status != "active":
        return status
    if inbox_rate is None or landed < 5:
        return "starting"
    return "healthy" if inbox_rate >= 0.8 else "fair" if inbox_rate >= 0.5 else "poor"


def _local_date(value, tz: ZoneInfo) -> date | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(tz).date()


# ---------- SMTP / IMAP ----------

class MailboxError(Exception):
    """A problem with the mailbox itself. `transient` (server unreachable) is retried; a wrong password sets 'error'."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _smtp(mb: dict, password: str) -> smtplib.SMTP:
    try:
        if int(mb["smtp_port"]) == 465:
            server = smtplib.SMTP_SSL(mb["smtp_host"], 465, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(mb["smtp_host"], int(mb["smtp_port"]), timeout=TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        server.login(mb["username"], password)
        return server
    except smtplib.SMTPAuthenticationError:
        raise MailboxError("Sending failed: the email or password is wrong.")
    except (socket.timeout, OSError, smtplib.SMTPException) as e:
        raise MailboxError(f"Couldn't reach the sending server {mb['smtp_host']}:{mb['smtp_port']} ({e}).", transient=True)


def _imap(mb: dict, password: str) -> imaplib.IMAP4_SSL:
    try:
        imap = imaplib.IMAP4_SSL(mb["imap_host"], int(mb["imap_port"]), timeout=TIMEOUT)
    except (socket.timeout, OSError) as e:
        raise MailboxError(f"Couldn't reach the inbox server {mb['imap_host']}:{mb['imap_port']} ({e}).", transient=True)
    try:
        imap.login(mb["username"], password)
    except imaplib.IMAP4.error:
        raise MailboxError("Reading the inbox failed: the email or password is wrong.")
    return imap


LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"(?P<delim>[^"]*)"|NIL) (?P<name>.+)$')
SPAM_NAMES = {"spam", "junk", "junk e-mail", "junk email", "bulk mail", "bulk"}


def spam_folder(list_lines: list[bytes]) -> str | None:
    """The spam folder's name from an IMAP LIST answer: the one marked \\Junk, else one called Spam / Junk."""
    by_name = None
    for line in list_lines:
        m = LIST_RE.match(line or b"")
        if not m:
            continue
        name = m.group("name").decode(errors="replace").strip().strip('"')
        if b"\\junk" in m.group("flags").lower():
            return name
        delim = (m.group("delim") or b"").decode() or "/"
        if by_name is None and name.split(delim)[-1].strip().lower() in SPAM_NAMES:
            by_name = name
    return by_name


def search_uids(data) -> list[bytes]:
    """The UIDs in an IMAP SEARCH answer. Some servers (e.g. AgentMail) put the word SEARCH or other tokens in it, and
    the answer can be spread over several items: keep only the numbers."""
    out: list[bytes] = []
    for item in data or []:
        if isinstance(item, tuple):
            item = item[-1]
        if isinstance(item, bytes):
            out += [t for t in item.split() if t.isdigit() and t not in out]
    return out


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def test_connection(mb: dict, password: str) -> dict:
    """Logs in to both servers. Returns {spam_folder}; raises MailboxError with a readable message."""
    _smtp(mb, password).quit()
    imap = _imap(mb, password)
    try:
        _, lines = imap.list()
        return {"spam_folder": spam_folder(lines or [])}
    finally:
        _logout(imap)


def build_message(sender: dict, recipient: dict, row: dict, parent_message_id: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender.get("display_name") or "", sender["email"]))
    msg["To"] = formataddr((recipient.get("display_name") or "", recipient["email"]))
    msg["Subject"] = row["subject"]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = row["message_id"]
    msg[HEADER] = row["id"]
    if parent_message_id:
        msg["In-Reply-To"] = parent_message_id
        msg["References"] = parent_message_id
    msg.set_content(row["body"])
    return msg


def _logout(imap):
    try:
        imap.logout()
    except Exception:
        pass


# ---------- Mailboxes (API) ----------

PUBLIC_FIELDS = "id, user_id, email, display_name, smtp_host, smtp_port, imap_host, imap_port, username, status, warmup_started_on, daily_limit, warmup_increase, warmup_reply_rate, warmup_weekdays_only, warmup_settings, last_error, last_checked_at, created_at"


def list_mailboxes(user_id: str) -> list[dict]:
    return _db().table("mailboxes").select(PUBLIC_FIELDS).eq("user_id", user_id).order("created_at").execute().data


def get_mailbox(user_id: str, mailbox_id: str, with_password: bool = False) -> dict | None:
    fields = PUBLIC_FIELDS + (", password_enc" if with_password else "")
    rows = _db().table("mailboxes").select(fields).eq("user_id", user_id).eq("id", mailbox_id).limit(1).execute().data
    return rows[0] if rows else None


def overview(user_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = _tz()
    today = now.astimezone(tz).date()
    mailboxes = list_mailboxes(user_id)
    messages = []
    if mailboxes:
        since = (today - timedelta(days=HISTORY_DAYS + 1)).isoformat()
        messages = (_db().table("warmup_messages")
                    .select("from_mailbox_id, to_mailbox_id, kind, plan_date, status, scheduled_at, sent_at, found_at, landed_in, rescued, error")
                    .eq("user_id", user_id).gte("plan_date", since).limit(10000).execute().data)
    rows = summarize(mailboxes, messages, today, tz, days=HISTORY_DAYS)
    active = [r for r in rows if r["status"] == "active"]
    inbox = sum(r["inbox_7d"] for r in rows)
    spam = sum(r["spam_7d"] for r in rows)
    return {
        "timezone": settings.WARMUP_TIMEZONE,
        "window": {"start": WINDOW[0], "end": WINDOW[1]},
        "ramp": [{"until_day": d, "per_day": n} for d, n in RAMP],
        "max_mailboxes": MAX_MAILBOXES,
        "defaults": HOSTINGER,
        "summary": {
            "mailboxes": len(rows),
            "active": len(active),
            "paused": sum(1 for r in rows if r["status"] == "paused"),
            "failing": sum(1 for r in rows if r["status"] == "error"),
            "failed_7d": sum(r["failed_7d"] for r in rows),
            "spam_rate_7d": round(spam / (inbox + spam), 3) if inbox + spam else None,
            "received_7d": sum(r["received_7d"] for r in rows),
            "sent_7d": sum(r["sent_7d"] for r in rows),
            "sent_today": sum(r["sent_today"] for r in rows),
            "inbox_7d": inbox,
            "spam_7d": spam,
            "inbox_rate_7d": round(inbox / (inbox + spam), 3) if inbox + spam else None,
            "rescued_7d": sum(r["rescued_7d"] for r in rows),
        },
        "mailboxes": rows,
    }


DKIM_SELECTORS = ["hostingermail1", "hostingermail-a", "hostingermail-b", "hostingermail-c", "default", "google", "selector1", "selector2", "resend"]


def domain_check(email: str, resolver=None) -> dict:
    """SPF, DMARC and DKIM records of a mailbox's domain. DKIM selectors differ by provider, so only common ones are tried:
    not finding one is a hint, not proof it's missing."""
    from app.services import preflight

    domain = email.rsplit("@", 1)[-1].lower()
    resolver = resolver or preflight.dns_txt
    spf = [t for t in resolver(domain) if t.lower().startswith("v=spf1")]
    dmarc = [t for t in resolver(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    selector = None
    for name in DKIM_SELECTORS:
        try:
            if any("p=" in t for t in resolver(f"{name}._domainkey.{domain}")):
                selector = name
                break
        except Exception:
            continue
    m = re.search(r"(?:^|;)\s*p=(\w+)", dmarc[0], re.I) if dmarc else None
    return {"domain": domain, "spf": bool(spf), "dkim": bool(selector), "dkim_selector": selector, "dmarc": bool(dmarc),
            "dmarc_policy": m.group(1).lower() if m else None}


TEST_CHECK_AFTER = 45  # seconds after a test email is sent before the receiving inbox is read


def send_test(user_id: str, mailbox_id: str, now: datetime | None = None, rng: random.Random | None = None) -> dict:
    """A warm-up email from this mailbox to another of the user's, due now (any time of day): for trying the warm-up
    without waiting for its window. The scheduler sends it within 30 s; check_test() reads the other inbox after."""
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random()
    boxes = list_mailboxes(user_id)
    me = next((m for m in boxes if m["id"] == mailbox_id), None)
    if not me:
        raise ValueError("Mailbox not found.")
    if me["status"] != "active":
        raise ValueError("Turn this mailbox's warm-up on first (the flame), then send a test.")
    others = [m for m in boxes if m["id"] != mailbox_id and m["status"] == "active"]
    if not others:
        raise ValueError("Add or turn on a second mailbox: a test email goes to one of your other mailboxes.")
    to = rng.choice(others)
    subject, body = warmup_content.compose_initial(
        rng, warmup_content.first_name(me.get("display_name"), me["email"]), warmup_content.first_name(to.get("display_name"), to["email"]),
        content_of(me))
    row = _db().table("warmup_messages").insert({
        "user_id": user_id, "from_mailbox_id": me["id"], "to_mailbox_id": to["id"], "kind": "initial",
        "plan_date": now.astimezone(clock(me, _tz())[0]).date().isoformat(), "subject": subject, "body": body,
        "scheduled_at": now.isoformat(),
    }).execute().data[0]
    return {"id": row["id"], "to": to["email"], "subject": subject}


def send_planned_now(user_id: str, mailbox_id: str, message_id: str, now: datetime | None = None) -> dict:
    """A planned warm-up email (or reply) of this mailbox goes out now instead of at its planned time."""
    now = now or datetime.now(timezone.utc)
    rows = (_db().table("warmup_messages").update({"scheduled_at": now.isoformat()})
            .eq("id", message_id).eq("user_id", user_id).eq("status", "scheduled")
            .or_(f"from_mailbox_id.eq.{mailbox_id},to_mailbox_id.eq.{mailbox_id}").execute().data)
    if not rows:
        raise ValueError("That email isn't waiting to be sent any more.")
    sender = next((m for m in list_mailboxes(user_id) if m["id"] == rows[0]["from_mailbox_id"]), None)
    if not sender or sender["status"] != "active":
        raise ValueError("The mailbox that sends it has its warm-up off. Turn it on first.")
    return {"id": rows[0]["id"], "subject": rows[0]["subject"]}


def check_test(user_id: str, message_id: str, wait: float = TEST_CHECK_AFTER):
    """After a test email: wait for it to go out, then read the receiving inbox so it shows as landed (inbox / spam)."""
    time.sleep(wait)
    db = _db()
    row = db.table("warmup_messages").select("to_mailbox_id, status").eq("id", message_id).limit(1).execute().data
    if not row or row[0]["status"] != "sent":
        time.sleep(wait)  # the scheduler may not have sent it yet
    mailboxes = db.table("mailboxes").select(PUBLIC_FIELDS + ", password_enc").eq("user_id", user_id).eq("status", "active").execute().data
    receiver = next((m for m in mailboxes if row and m["id"] == row[0]["to_mailbox_id"]), None)
    if receiver:
        try:
            check_inbox(receiver, mailboxes, datetime.now(timezone.utc))
        except Exception as e:  # the regular check a few minutes later picks it up anyway
            logger.warning("Test check failed for %s: %s", receiver["email"], e)


def activity(user_id: str, mailbox_id: str, limit: int = 25) -> list[dict]:
    db = _db()
    rows = (db.table("warmup_messages")
            .select("id, from_mailbox_id, to_mailbox_id, kind, subject, status, scheduled_at, sent_at, found_at, landed_in, rescued, error")
            .eq("user_id", user_id).or_(f"from_mailbox_id.eq.{mailbox_id},to_mailbox_id.eq.{mailbox_id}")
            .neq("status", "cancelled").order("scheduled_at", desc=True).limit(limit).execute().data)
    emails = {m["id"]: m["email"] for m in list_mailboxes(user_id)}
    for r in rows:
        r["direction"] = "out" if r["from_mailbox_id"] == mailbox_id else "in"
        r["other"] = emails.get(r["to_mailbox_id"] if r["direction"] == "out" else r["from_mailbox_id"], "removed mailbox")
    return rows


def add_mailbox(user_id: str, data: dict) -> dict:
    """Checks the login on both servers first, so a wrong password is caught now instead of at the first send."""
    db = _db()
    email = data["email"].strip().lower()
    existing = db.table("mailboxes").select("id", count="exact").eq("user_id", user_id).limit(1).execute()
    if (existing.count or 0) >= MAX_MAILBOXES:
        raise ValueError(f"You can warm up at most {MAX_MAILBOXES} mailboxes.")
    if db.table("mailboxes").select("id").eq("user_id", user_id).eq("email", email).limit(1).execute().data:
        raise ValueError(f"{email} is already added.")
    mb = {
        "email": email,
        "display_name": (data.get("display_name") or "").strip() or None,
        "smtp_host": (data.get("smtp_host") or HOSTINGER["smtp_host"]).strip(),
        "smtp_port": data.get("smtp_port") or HOSTINGER["smtp_port"],
        "imap_host": (data.get("imap_host") or HOSTINGER["imap_host"]).strip(),
        "imap_port": data.get("imap_port") or HOSTINGER["imap_port"],
        "username": (data.get("username") or email).strip(),
        "daily_limit": data.get("daily_limit") or 30,
    }
    found = test_connection(mb, data["password"])
    row = db.table("mailboxes").insert({**mb, "user_id": user_id, "password_enc": encrypt(data["password"])}).execute().data[0]
    row.pop("password_enc", None)
    return {**row, "spam_folder": found["spam_folder"]}


def update_mailbox(user_id: str, mailbox_id: str, data: dict) -> dict | None:
    mb = get_mailbox(user_id, mailbox_id, with_password=True)
    if not mb:
        return None
    changes = {k: v for k, v in data.items() if k in ("display_name", "daily_limit", "warmup_increase", "warmup_reply_rate", "warmup_weekdays_only") and v is not None}
    if data.get("warmup_settings"):
        new = {k: v for k, v in data["warmup_settings"].items() if k in ADVANCED_DEFAULTS and v is not None}
        if new.get("timezone"):
            try:
                ZoneInfo(new["timezone"])
            except Exception:
                raise ValueError(f"Unknown timezone: {new['timezone']}")
        merged = {**advanced(mb), **new}
        if merged["window_start"] == merged["window_end"]:
            raise ValueError("The sending window can't start and end at the same hour.")
        changes["warmup_settings"] = {**(mb.get("warmup_settings") or {}), **new}
    password = data.get("password")
    if password:
        test_connection(mb, password)
        changes.update(password_enc=encrypt(password), status="active", last_error=None)
    if data.get("status") == "paused":
        changes["status"] = "paused"
    elif data.get("status") == "active" and not password:
        test_connection(mb, decrypt(mb["password_enc"]))  # resuming re-checks the login
        changes.update(status="active", last_error=None)
    if not changes:
        mb.pop("password_enc", None)
        return mb
    changes["updated_at"] = datetime.now(timezone.utc).isoformat()
    row = _db().table("mailboxes").update(changes).eq("id", mailbox_id).eq("user_id", user_id).execute().data[0]
    if changes.get("status") == "paused":
        _cancel_scheduled(mailbox_id)
    row.pop("password_enc", None)
    return row


def delete_mailbox(user_id: str, mailbox_id: str) -> bool:
    return bool(_db().table("mailboxes").delete().eq("id", mailbox_id).eq("user_id", user_id).execute().data)


def _cancel_scheduled(mailbox_id: str):
    db = _db()
    for column in ("from_mailbox_id", "to_mailbox_id"):
        db.table("warmup_messages").update({"status": "cancelled"}).eq(column, mailbox_id).eq("status", "scheduled").execute()


def _set_error(mailbox_id: str, error: str):
    logger.warning("Warm-up mailbox %s: %s", mailbox_id, error)
    _db().table("mailboxes").update({"status": "error", "last_error": error[:500], "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", mailbox_id).execute()
    _cancel_scheduled(mailbox_id)


def _mailbox_failed(mailbox_id: str, error: Exception) -> bool:
    """Records a failure. Returns True if the mailbox was set to 'error', False if it will simply be tried again."""
    if getattr(error, "transient", False):
        _failures[mailbox_id] = _failures.get(mailbox_id, 0) + 1
        if _failures[mailbox_id] < FAILS_BEFORE_ERROR:
            logger.warning("Warm-up mailbox %s (try %s of %s): %s", mailbox_id, _failures[mailbox_id], FAILS_BEFORE_ERROR, error)
            return False
    _failures.pop(mailbox_id, None)
    _set_error(mailbox_id, str(error))
    return True


def _later(now: datetime, minutes: float = RETRY_MINUTES) -> str:
    return (now + timedelta(minutes=minutes)).isoformat()


def _send_tries(error: str | None) -> int:
    """How often an email was already tried: kept as a '[try N]' prefix of its error, so no extra column is needed."""
    m = re.match(r"\[try (\d+)\]", error or "")
    return int(m.group(1)) if m else 0


def office_hours(at: datetime, tz: ZoneInfo, rng: random.Random, window: tuple[int, int] = WINDOW) -> datetime:
    """`at` if it falls in office hours; else early in the next office hours (a reply isn't sent at 9 pm)."""
    local = at.astimezone(tz)
    if window[0] > window[1]:  # crosses midnight: open from the start hour until the end hour the next morning
        if local.hour >= window[0] or local.hour < window[1]:
            return at
        opens = datetime.combine(local.date(), datetime.min.time(), tz) + timedelta(hours=window[0])
        return (opens + timedelta(minutes=rng.uniform(0, 90))).astimezone(timezone.utc)
    opens = datetime.combine(local.date(), datetime.min.time(), tz) + timedelta(hours=window[0])
    if local.hour >= window[1]:
        opens += timedelta(days=1)
    elif local >= opens:
        return at
    return (opens + timedelta(minutes=rng.uniform(0, 90))).astimezone(timezone.utc)


# ---------- Scheduler ----------

def run(now: datetime | None = None, force: bool = False):
    """Called by the scheduler every 30 s: plans (every few minutes), sends what's due, reads the inboxes."""
    now = now or datetime.now(timezone.utc)
    mailboxes = _db().table("mailboxes").select(PUBLIC_FIELDS + ", password_enc").eq("status", "active").execute().data
    if not mailboxes:
        return
    clock = time.monotonic()
    if force or clock - _last["plan"] >= PLAN_SECONDS:
        _last["plan"] = clock
        plan(mailboxes, now)
    send_due(mailboxes, now)
    if force or clock - _last["check"] >= CHECK_SECONDS:
        _last["check"] = clock
        for mb in mailboxes:
            try:
                check_inbox(mb, mailboxes, now)
                _failures.pop(mb["id"], None)
            except MailboxError as e:
                _mailbox_failed(mb["id"], e)
            except Exception as e:
                logger.exception("Warm-up inbox check failed for %s: %s", mb["email"], e)


def plan(mailboxes: list[dict], now: datetime, rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    tz = _tz()
    today = now.astimezone(tz).date()
    db = _db()
    by_user: dict[str, list[dict]] = {}
    for mb in mailboxes:
        by_user.setdefault(mb["user_id"], []).append(mb)
    created = 0
    for user_id, mine in by_user.items():
        if len(mine) < 2:
            continue
        # Mailboxes can be in other timezones, so their "today" is yesterday, today or tomorrow here
        dates = [(today + timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
        planned = (db.table("warmup_messages").select("from_mailbox_id, plan_date").eq("user_id", user_id).eq("kind", "initial")
                   .in_("plan_date", dates).limit(5000).execute().data)
        local_today = {mb["id"]: window_bounds(now, *clock(mb, tz))[0].isoformat() for mb in mine}
        already: dict[str, int] = {}
        for r in planned:
            if str(r["plan_date"])[:10] == local_today.get(r["from_mailbox_id"]):
                already[r["from_mailbox_id"]] = already.get(r["from_mailbox_id"], 0) + 1
        rows = []
        for s in plan_day(mine, already, now, rng, tz):
            subject, body = warmup_content.compose_initial(
                rng, warmup_content.first_name(s["from"].get("display_name"), s["from"]["email"]),
                warmup_content.first_name(s["to"].get("display_name"), s["to"]["email"]), content_of(s["from"]))
            rows.append({
                "user_id": user_id, "from_mailbox_id": s["from"]["id"], "to_mailbox_id": s["to"]["id"], "kind": "initial",
                "plan_date": s["plan_date"].isoformat(), "subject": subject, "body": body, "scheduled_at": s["at"].isoformat(),
            })
        if rows:
            db.table("warmup_messages").insert(rows).execute()
            created += len(rows)
    if created:
        print(f"[warm-up] planned {created} emails", flush=True)
    return created


def send_due(mailboxes: list[dict], now: datetime) -> int:
    db = _db()
    due = (db.table("warmup_messages").select("*").eq("status", "scheduled").lte("scheduled_at", now.isoformat())
           .order("scheduled_at").limit(SEND_PER_TICK).execute().data)
    if not due:
        return 0
    active = {m["id"]: m for m in mailboxes}
    parents = {}
    parent_ids = [r["parent_id"] for r in due if r.get("parent_id")]
    if parent_ids:
        parents = {p["id"]: p["message_id"] for p in db.table("warmup_messages").select("id, message_id").in_("id", parent_ids).execute().data}
    sent = 0
    servers: dict[str, smtplib.SMTP] = {}
    postponed: set[str] = set()  # senders whose server couldn't be reached this tick: their emails wait a few minutes
    table = lambda: db.table("warmup_messages")  # noqa: E731
    try:
        for row in due:
            sender, recipient = active.get(row["from_mailbox_id"]), active.get(row["to_mailbox_id"])
            if not sender or not recipient:
                table().update({"status": "cancelled"}).eq("id", row["id"]).execute()
                continue
            if sender["id"] in postponed:
                table().update({"scheduled_at": _later(now)}).eq("id", row["id"]).execute()
                continue
            # Claim the email before sending, so a second copy of the scheduler can't send it too
            if not table().update({"status": "sent"}).eq("id", row["id"]).eq("status", "scheduled").execute().data:
                continue
            row["message_id"] = make_msgid(idstring=uuid.uuid4().hex[:12], domain=sender["email"].split("@")[1])
            try:
                if sender["id"] not in servers:
                    servers[sender["id"]] = _smtp(sender, decrypt(sender["password_enc"]))
                servers[sender["id"]].send_message(build_message(sender, recipient, row, parents.get(row.get("parent_id"))))
            except (MailboxError, ValueError) as e:
                if isinstance(e, MailboxError) and not _mailbox_failed(sender["id"], e):
                    postponed.add(sender["id"])
                    table().update({"status": "scheduled", "scheduled_at": _later(now)}).eq("id", row["id"]).execute()
                else:
                    if isinstance(e, ValueError):
                        _set_error(sender["id"], str(e))
                    active.pop(sender["id"], None)
                    table().update({"status": "cancelled", "error": str(e)[:500]}).eq("id", row["id"]).execute()
                continue
            except Exception as e:
                tries = _send_tries(row.get("error")) + 1
                error = f"[try {tries}] {e}"[:500]
                if tries < MAX_SEND_TRIES:
                    table().update({"status": "scheduled", "scheduled_at": _later(now), "error": error}).eq("id", row["id"]).execute()
                else:
                    table().update({"status": "failed", "error": error}).eq("id", row["id"]).execute()
                servers.pop(sender["id"], None)
                continue
            _failures.pop(sender["id"], None)
            table().update({"status": "sent", "sent_at": datetime.now(timezone.utc).isoformat(),
                            "message_id": row["message_id"], "error": None}).eq("id", row["id"]).execute()
            sent += 1
    finally:
        for server in servers.values():
            try:
                server.quit()
            except Exception:
                pass
    if sent:
        print(f"[warm-up] sent {sent}", flush=True)
    return sent


FIND_FALLBACK_LIMIT = 300  # newest emails looked at when the server can't search by header
HEADER_ID_RE = re.compile(rb"X-Warmup-Id:\s*([0-9a-f-]{36})", re.I)


def _find(imap, since: str) -> dict[str, bytes]:
    """{warm-up id: uid} of the warm-up emails in the selected folder."""
    typ, data = imap.uid("search", None, "SINCE", since, "HEADER", HEADER, '""')
    if typ != "OK":
        # Some servers (e.g. AgentMail) can't search by header: take the recent emails and read the header of each
        _, data = imap.uid("search", None, "SINCE", since)
        uids = search_uids(data)[-FIND_FALLBACK_LIMIT:]
    else:
        uids = search_uids(data)
    if not uids:
        return {}
    _, parts = imap.uid("fetch", b",".join(uids), f"(BODY.PEEK[HEADER.FIELDS ({HEADER.upper()})])")
    found = {}
    for part in parts or []:
        if not isinstance(part, tuple):
            continue
        uid = re.search(rb"UID (\d+)", part[0])
        hid = HEADER_ID_RE.search(part[1] or b"")
        if uid and hid:
            found[hid.group(1).decode().lower()] = uid.group(1)
    return found


def check_inbox(mb: dict, mailboxes: list[dict], now: datetime, rng: random.Random | None = None) -> dict:
    """Reads one mailbox's Inbox and Spam: records each new warm-up email, rescues it from spam, reads it, answers some."""
    rng = rng or random.Random()
    db = _db()
    imap = _imap(mb, decrypt(mb["password_enc"]))
    result = {"inbox": 0, "spam": 0, "replies": 0}
    try:
        _, lines = imap.list()
        junk = spam_folder(lines or [])
        since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        for folder, landed in (("INBOX", "inbox"), (junk, "spam")):
            if not folder or imap.select(_quote(folder))[0] != "OK":
                continue
            found = _find(imap, since)
            if not found:
                continue
            rows = (db.table("warmup_messages").select("id, from_mailbox_id, to_mailbox_id, kind, subject")
                    .in_("id", list(found)).eq("to_mailbox_id", mb["id"]).is_("found_at", "null").execute().data)
            adv = advanced(mb)
            for row in rows:
                uid = found[row["id"]]
                if rng.random() < adv["open_rate"] / 100:
                    if adv["read_emulation"]:
                        imap.uid("fetch", uid, "(BODY[])")  # reads the whole email, as a mail app does when it's opened
                    imap.uid("store", uid, "+FLAGS", "(\\Seen)")
                if rng.random() < adv["important_rate"] / 100:
                    imap.uid("store", uid, "+FLAGS", "(\\Flagged)")
                rescued = False
                if landed == "spam" and rng.random() < adv["spam_protection"] / 100:
                    rescued = _move_to_inbox(imap, uid)
                # Only the first reader records it, so a test check and the regular check never both answer it
                claimed = db.table("warmup_messages").update({
                    "landed_in": landed, "found_at": now.isoformat(), "rescued": rescued,
                }).eq("id", row["id"]).is_("found_at", "null").execute().data
                if not claimed:
                    continue
                result[landed] += 1
                if row["kind"] == "initial" and rng.random() < reply_rate(mb):
                    result["replies"] += _schedule_reply(row, mb, mailboxes, now, rng)
            if landed == "spam":
                result["rescued_later"] = result.get("rescued_later", 0) + _rescue_found(imap, mb, found, rng)
            result["chat"] = result.get("chat", 0) + _handle_chat(imap, mb, found, landed, now)
        try:
            from app.services import newsletters

            result["newsletters"] = newsletters.process(imap, mb, junk, now, _move_to_inbox, _quote)
        except Exception as e:  # newsletters are extra: a problem here must not stop the warm-up
            logger.warning("Newsletter check failed for %s: %s", mb["email"], e)
    finally:
        _logout(imap)
    db.table("mailboxes").update({"last_checked_at": now.isoformat()}).eq("id", mb["id"]).execute()
    if result["inbox"] or result["spam"]:
        print(f"[warm-up] {mb['email']}: {result['inbox']} in inbox, {result['spam']} rescued from spam, {result['replies']} replies planned", flush=True)
    return result


def _rescue_found(imap, mb: dict, found: dict[str, bytes], rng: random.Random) -> int:
    """Warm-up emails in the spam folder that were already recorded (in the inbox, earlier) - put in spam later by the
    provider or by a person - are moved back to the inbox too. Spam folder must be selected."""
    if not found:
        return 0
    rows = (_db().table("warmup_messages").select("id, found_at").in_("id", list(found)).eq("to_mailbox_id", mb["id"])
            .eq("rescued", False).execute().data)
    share = advanced(mb)["spam_protection"] / 100
    moved = 0
    for row in rows:
        if not row.get("found_at") or rng.random() >= share:
            continue  # not seen before (handled above), or left in spam by the spam-protection setting
        if _move_to_inbox(imap, found[row["id"]]):
            claimed = _db().table("warmup_messages").update({"rescued": True}).eq("id", row["id"]).eq("rescued", False).execute().data
            moved += 1 if claimed else 0
    if moved:
        print(f"[warm-up] {mb['email']}: {moved} earlier warm-up email(s) found in spam, moved back to the inbox", flush=True)
    return moved


def _handle_chat(imap, mb: dict, found: dict[str, bytes], landed: str, now: datetime) -> int:
    """Dashboard chat messages (mailbox_chat) in the selected folder: read, rescued from spam, receipt recorded."""
    try:
        from app.services import mailbox_chat

        pending = mailbox_chat.pending_receipts(list(found), mb["id"])
    except Exception as e:  # chat tables missing or a database hiccup: the warm-up goes on
        logger.warning("Chat check failed for %s: %s", mb["email"], e)
        return 0
    for rec in pending:
        uid = found[rec["message_id"]]
        imap.uid("store", uid, "+FLAGS", "(\\Seen)")
        rescued = _move_to_inbox(imap, uid) if landed == "spam" else False
        mailbox_chat.record_receipt(rec["message_id"], mb["id"], landed, rescued, now)
    return len(pending)


def _capabilities(imap) -> set[bytes]:
    """What the server supports. Asked again after login: imaplib only keeps the greeting's list, and servers such as
    Hostinger's name MOVE and UIDPLUS only once logged in."""
    cached = getattr(imap, "_warmup_caps", None)
    if cached is not None:
        return cached
    caps = {(c if isinstance(c, bytes) else c.encode()).upper() for c in imap.capabilities}
    try:
        typ, data = imap.capability()
        if typ == "OK" and data and data[0]:
            caps |= set(data[0].upper().split())
    except Exception:
        pass
    try:
        imap._warmup_caps = caps
    except Exception:
        pass
    return caps


def _move_to_inbox(imap, uid: bytes) -> bool:
    """Moves one email from the selected (spam) folder to the Inbox. Never expunges the whole folder: a plain EXPUNGE
    would also erase anything else in Spam the user had marked deleted."""
    try:
        caps = _capabilities(imap)
        if b"MOVE" in caps:
            return imap.uid("move", uid, "INBOX")[0] == "OK"
        if imap.uid("copy", uid, "INBOX")[0] == "OK":
            imap.uid("store", uid, "+FLAGS", "(\\Deleted)")
            if b"UIDPLUS" in caps:
                imap.uid("expunge", uid)  # removes only this email
            return True
    except Exception as e:
        logger.warning("Couldn't move a warm-up email out of spam: %s", e)
    return False


def _schedule_reply(original: dict, me: dict, mailboxes: list[dict], now: datetime, rng: random.Random) -> int:
    partner = next((m for m in mailboxes if m["id"] == original["from_mailbox_id"]), None)
    if not partner:
        return 0
    subject, body = warmup_content.compose_reply(
        rng, original["subject"],
        warmup_content.first_name(me.get("display_name"), me["email"]),
        warmup_content.first_name(partner.get("display_name"), partner["email"]))
    tz, w_start, w_end = clock(me, _tz())
    delay = REPLY_DELAYS.get(advanced(me).get("reply_delay"), REPLY_DELAY_MIN)
    at = office_hours(now + timedelta(minutes=rng.uniform(*delay)), tz, rng, (w_start, w_end))
    _db().table("warmup_messages").insert({
        "user_id": me["user_id"], "from_mailbox_id": me["id"], "to_mailbox_id": partner["id"], "kind": "reply",
        "parent_id": original["id"], "plan_date": window_bounds(at, tz, w_start, w_end)[0].isoformat(),
        "subject": subject, "body": body, "scheduled_at": at.isoformat(),
    }).execute()
    return 1
