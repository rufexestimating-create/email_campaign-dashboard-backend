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
WINDOW = (9, 18)                # office hours, local time (WARMUP_TIMEZONE)
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


def daily_target(day: int, limit: int) -> int:
    """Emails a mailbox sends on its `day`th day of warm-up: 3 a day at first, rising to its daily limit."""
    for last_day, per_day in RAMP:
        if day <= last_day:
            return min(per_day, limit)
    return limit


def plan_day(mailboxes: list[dict], already: dict[str, int], now: datetime, rng: random.Random, tz: ZoneInfo) -> list[dict]:
    """Today's warm-up emails for mailboxes that have none planned yet: [{from, to, at}] (UTC times).

    `already` counts each mailbox's emails planned for today, so a mailbox added in the afternoon still gets its share
    and one that's planned isn't planned twice. Times fall in what's left of today's office hours.
    """
    if len(mailboxes) < 2:
        return []
    local = now.astimezone(tz)
    today = local.date()
    start = max(local + timedelta(minutes=2), datetime.combine(today, datetime.min.time(), tz).replace(hour=WINDOW[0]))
    end = datetime.combine(today, datetime.min.time(), tz).replace(hour=WINDOW[1])
    if start >= end:
        return []
    span = (end - start).total_seconds()
    sends = []
    for mb in mailboxes:
        if already.get(mb["id"]):
            continue
        target = daily_target(warmup_day(_as_date(mb["warmup_started_on"]), today), mb["daily_limit"])
        # The share of today still left, so a mailbox planned at 4 pm doesn't fit a whole day into two hours
        target = max(1, round(target * min(1.0, span / ((WINDOW[1] - WINDOW[0]) * 3600))))
        others = [m for m in mailboxes if m["id"] != mb["id"]]
        for _ in range(target):
            at = start + timedelta(seconds=rng.uniform(0, span))
            sends.append({"from": mb, "to": rng.choice(others), "at": at.astimezone(timezone.utc)})
    return sorted(sends, key=lambda s: s["at"])


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


# ---------- Pure: report ----------

def summarize(mailboxes: list[dict], messages: list[dict], today: date, tz: ZoneInfo, days: int = 14) -> list[dict]:
    """Per mailbox: warm-up day, today's progress, and where its emails landed (last 7 days, plus a daily series)."""
    stats = {
        m["id"]: {
            "sent_today": 0, "received_today": 0, "scheduled_today": 0,
            "inbox_7d": 0, "spam_7d": 0, "rescued_7d": 0, "replies_7d": 0, "waiting": 0,
            "series": {(today - timedelta(days=i)).isoformat(): {"sent": 0, "inbox": 0, "spam": 0} for i in range(days - 1, -1, -1)},
        }
        for m in mailboxes
    }
    week_ago = today - timedelta(days=6)
    for msg in messages:
        sender, receiver = stats.get(msg["from_mailbox_id"]), stats.get(msg["to_mailbox_id"])
        sent_day = _local_date(msg.get("sent_at"), tz)
        found_day = _local_date(msg.get("found_at"), tz)
        if sender:
            if msg["status"] == "scheduled" and _as_date(msg["plan_date"]) == today:
                sender["scheduled_today"] += 1
            if msg["status"] == "sent":
                if sent_day == today:
                    sender["sent_today"] += 1
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
            "target_today": daily_target(day, mb["daily_limit"]),
            "sent_today": s["sent_today"],
            "scheduled_today": s["scheduled_today"],
            "received_today": s["received_today"],
            "inbox_7d": s["inbox_7d"],
            "spam_7d": s["spam_7d"],
            "inbox_rate_7d": inbox_rate,
            "rescued_7d": s["rescued_7d"],
            "replies_7d": s["replies_7d"],
            "waiting": s["waiting"],
            "health": _health(mb["status"], inbox_rate, landed),
            "series": [{"date": d, **v} for d, v in s["series"].items()],
            "smtp_host": mb.get("smtp_host"),
            "imap_host": mb.get("imap_host"),
        })
    return out


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

PUBLIC_FIELDS = "id, user_id, email, display_name, smtp_host, smtp_port, imap_host, imap_port, username, status, warmup_started_on, daily_limit, last_error, last_checked_at, created_at"


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
        since = (today - timedelta(days=14)).isoformat()
        messages = (_db().table("warmup_messages")
                    .select("from_mailbox_id, to_mailbox_id, kind, plan_date, status, sent_at, found_at, landed_in, rescued")
                    .eq("user_id", user_id).gte("plan_date", since).limit(10000).execute().data)
    rows = summarize(mailboxes, messages, today, tz)
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
            "sent_today": sum(r["sent_today"] for r in rows),
            "inbox_7d": inbox,
            "spam_7d": spam,
            "inbox_rate_7d": round(inbox / (inbox + spam), 3) if inbox + spam else None,
            "rescued_7d": sum(r["rescued_7d"] for r in rows),
        },
        "mailboxes": rows,
    }


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
    changes = {k: v for k, v in data.items() if k in ("display_name", "daily_limit") and v is not None}
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


def office_hours(at: datetime, tz: ZoneInfo, rng: random.Random) -> datetime:
    """`at` if it falls in office hours; else early in the next office hours (a reply isn't sent at 9 pm)."""
    local = at.astimezone(tz)
    opens = datetime.combine(local.date(), datetime.min.time(), tz).replace(hour=WINDOW[0])
    if local.hour >= WINDOW[1]:
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
        planned = (db.table("warmup_messages").select("from_mailbox_id").eq("user_id", user_id).eq("kind", "initial")
                   .eq("plan_date", today.isoformat()).limit(5000).execute().data)
        already: dict[str, int] = {}
        for r in planned:
            already[r["from_mailbox_id"]] = already.get(r["from_mailbox_id"], 0) + 1
        rows = []
        for s in plan_day(mine, already, now, rng, tz):
            subject, body = warmup_content.compose_initial(
                rng, warmup_content.first_name(s["from"].get("display_name"), s["from"]["email"]),
                warmup_content.first_name(s["to"].get("display_name"), s["to"]["email"]))
            rows.append({
                "user_id": user_id, "from_mailbox_id": s["from"]["id"], "to_mailbox_id": s["to"]["id"], "kind": "initial",
                "plan_date": today.isoformat(), "subject": subject, "body": body, "scheduled_at": s["at"].isoformat(),
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


HEADER_ID_RE = re.compile(rb"X-Warmup-Id:\s*([0-9a-f-]{36})", re.I)


def _find(imap, since: str) -> dict[str, bytes]:
    """{warm-up id: uid} of the warm-up emails in the selected folder."""
    _, data = imap.uid("search", None, "SINCE", since, "HEADER", HEADER, '""')
    uids = (data[0] or b"").split() if data else []
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
            for row in rows:
                uid = found[row["id"]]
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
                if rng.random() < STAR_RATE:
                    imap.uid("store", uid, "+FLAGS", "(\\Flagged)")
                rescued = False
                if landed == "spam":
                    rescued = _move_to_inbox(imap, uid)
                db.table("warmup_messages").update({
                    "landed_in": landed, "found_at": now.isoformat(), "rescued": rescued,
                }).eq("id", row["id"]).execute()
                result[landed] += 1
                if row["kind"] == "initial" and rng.random() < REPLY_RATE:
                    result["replies"] += _schedule_reply(row, mb, mailboxes, now, rng)
    finally:
        _logout(imap)
    db.table("mailboxes").update({"last_checked_at": now.isoformat()}).eq("id", mb["id"]).execute()
    if result["inbox"] or result["spam"]:
        print(f"[warm-up] {mb['email']}: {result['inbox']} in inbox, {result['spam']} rescued from spam, {result['replies']} replies planned", flush=True)
    return result


def _capabilities(imap) -> set[bytes]:
    return {(c if isinstance(c, bytes) else c.encode()).upper() for c in imap.capabilities}


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
    tz = _tz()
    at = office_hours(now + timedelta(minutes=rng.uniform(*REPLY_DELAY_MIN)), tz, rng)
    _db().table("warmup_messages").insert({
        "user_id": me["user_id"], "from_mailbox_id": me["id"], "to_mailbox_id": partner["id"], "kind": "reply",
        "parent_id": original["id"], "plan_date": at.astimezone(tz).date().isoformat(),
        "subject": subject, "body": body, "scheduled_at": at.isoformat(),
    }).execute()
    return 1
