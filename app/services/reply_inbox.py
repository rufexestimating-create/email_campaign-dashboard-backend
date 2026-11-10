"""Replies, read from the reply mailbox over IMAP.

Resend only sends; a reply goes to the Reply-To mailbox (REPLY_TO_EMAIL, e.g. a Hostinger inbox). The scheduler calls
poll() every minute: it reads the last few days of that inbox read-only (nothing is marked read, moved or deleted),
and each message from someone we emailed becomes a "replied" event on the latest email sent to them, plus a row in
email_replies with what they wrote. A reply also stops that person's follow-ups (follow_ups.stop_reason).
"""
import email
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html import unescape

from app.core.config import settings
from app.db.supabase import get_supabase
from app.services import event_tracking

logger = logging.getLogger(__name__)

POLL_SECONDS = 60
LOOKBACK_DAYS = 3
SNIPPET_LENGTH = 500
BODY_LENGTH = 20000
IMAGE_TAG = re.compile(r"\[image:[^\]]*\]", re.I)  # Gmail's plain text marks inline images like this
# Out-of-office and other automatic answers aren't real replies: they mustn't stop follow-ups
AUTO_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}
SYSTEM_SENDERS = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounces?)@", re.I)
# Where the quoted original starts in a reply: "On Mon, 2 Nov 2026 ... wrote:" (Gmail wraps it over two lines when the
# name is long, but never across a blank line), Outlook's header block, or "Sent from my iPhone"
QUOTE_START = re.compile(
    r"^(On\s(?:(?!\n[ \t]*\n).){0,300}?\bwrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}|From: .+|Sent from my \w+)[ \t]*$",
    re.I | re.M | re.S,
)
# Gmail's plain-text part writes bold words as *word*
BOLD_RE = re.compile(r"\*([^*\n]+)\*")
TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>")

_last_poll = 0.0


def is_configured() -> bool:
    return bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD)


def _header(msg: Message, name: str) -> str:
    value = msg.get(name)
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def is_automatic(msg: Message) -> bool:
    auto = _header(msg, "Auto-Submitted").lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Auto-Response-Suppress", "").lower() in ("all", "oof"):
        return True
    if _header(msg, "Precedence").lower() in AUTO_PRECEDENCE:
        return True
    return bool(SYSTEM_SENDERS.match(parseaddr(_header(msg, "From"))[1]))


def _body_text(msg: Message) -> str:
    """The plain-text part, or the HTML part with its tags removed."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain = html = None
    for part in parts:
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").lower().startswith("attachment"):
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_subtype() == "plain" and plain is None:
            plain = text
        elif part.get_content_subtype() == "html" and html is None:
            html = text
    if plain is not None:
        return plain
    return unescape(TAG_RE.sub(" ", html or ""))


def reply_text(body: str) -> str:
    """What they wrote, without the quoted original email below it."""
    body = body.replace("\r\n", "\n")
    m = QUOTE_START.search(body)
    if m:
        body = body[:m.start()]
    lines = [l for l in body.splitlines() if not l.lstrip().startswith(">")]
    return BOLD_RE.sub(r"\1", re.sub(r"\s+", " ", "\n".join(lines))).strip()[:SNIPPET_LENGTH]


def full_text(body: str) -> str:
    """Everything they wrote, with its line breaks, without the quoted original email below it."""
    body = body.replace("\r\n", "\n")
    m = QUOTE_START.search(body)
    if m:
        body = body[:m.start()]
    lines = [l.rstrip() for l in body.splitlines() if not l.lstrip().startswith(">")]
    text = IMAGE_TAG.sub("", BOLD_RE.sub(r"\1", "\n".join(lines)))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:BODY_LENGTH]


def parse(raw: bytes, now: datetime | None = None) -> dict | None:
    """Raw message -> the fields we keep, or None for automatic messages and messages without a sender."""
    msg = email.message_from_bytes(raw)
    name, address = parseaddr(_header(msg, "From"))
    if not address or is_automatic(msg):
        return None
    try:
        received = event_tracking.as_utc(parsedate_to_datetime(_header(msg, "Date")))
    except (TypeError, ValueError):
        received = now or datetime.now(timezone.utc)
    message_id = _header(msg, "Message-ID") or f"<no-id {address} {received.isoformat()}>"
    return {
        "from_email": address.lower(),
        "from_name": name or None,
        "subject": _header(msg, "Subject")[:255],
        "message_id": message_id[:500],
        "received_at": received,
        "snippet": reply_text(_body_text(msg)),
        "body": full_text(_body_text(msg)),
        "to": [a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))],
    }


def _sent_to(db, address: str, before: datetime) -> dict | None:
    """The latest email we sent to this address before the reply arrived."""
    rows = (db.table("email_logs").select("id, user_id, recipient, campaign_id, variant, last_event")
            .eq("recipient", address).in_("status", ["sent", "simulated"]).lte("sent_at", before.isoformat())
            .order("sent_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def record(db, reply: dict) -> bool:
    """Stores one reply; False if it isn't from anyone we emailed, or was stored before."""
    log = _sent_to(db, reply["from_email"], reply["received_at"])
    if not log:
        return False
    row = {
        "user_id": log["user_id"], "email_id": log["id"], "campaign_id": log.get("campaign_id"), "recipient": log["recipient"],
        "from_name": reply["from_name"], "subject": reply["subject"], "snippet": reply["snippet"], "body": reply.get("body"),
        "message_id": reply["message_id"], "received_at": reply["received_at"].isoformat(),
    }
    # message_id is unique: a message read again on the next poll inserts nothing
    if not db.table("email_replies").upsert(row, on_conflict="message_id", ignore_duplicates=True).execute().data:
        return False
    db.table("email_events").insert({
        "user_id": log["user_id"], "email_id": log["id"], "campaign_id": log.get("campaign_id"), "variant": log.get("variant"),
        "recipient": log["recipient"], "lead_id": event_tracking._lead_id(db, log["user_id"], log["recipient"]),
        "event_type": "replied", "occurred_at": reply["received_at"].isoformat(), "source": "inbox",
        "data": {"subject": reply["subject"]},
    }).execute()
    event_tracking._advance_last_event(db, log, "replied")
    return True


def _known_senders(db, addresses: set[str]) -> set[str]:
    found: set[str] = set()
    items = sorted(addresses)
    for i in range(0, len(items), 200):
        rows = db.table("email_logs").select("recipient").in_("recipient", items[i:i + 200]).limit(5000).execute().data
        found |= {r["recipient"] for r in rows}
    return found


def poll(now: datetime | None = None, force: bool = False) -> int:
    """Reads the reply mailbox; returns how many new replies were recorded. Runs at most once a minute."""
    global _last_poll
    if not is_configured() or (not force and time.monotonic() - _last_poll < POLL_SECONDS):
        return 0
    _last_poll = time.monotonic()
    now = now or datetime.now(timezone.utc)
    db = get_supabase()
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=30)
    try:
        imap.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        imap.select("INBOX", readonly=True)  # read-only: nothing is marked read, moved or deleted
        _, data = imap.uid("search", None, "SINCE", since)
        uids = [t for t in (data[0] or b"").split() if t.isdigit()] if data else []
        if not uids:
            return 0
        # Headers first: only messages from people we emailed, and not stored yet, are downloaded in full
        _, headers = imap.uid("fetch", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])")
        senders: dict[bytes, tuple[str, str]] = {}
        for part in headers:
            if not isinstance(part, tuple):
                continue
            uid = re.search(rb"UID (\d+)", part[0])
            if uid:
                h = email.message_from_bytes(part[1])
                senders[uid.group(1)] = (parseaddr(_header(h, "From"))[1].lower(), _header(h, "Message-ID"))
        known = _known_senders(db, {a for a, _ in senders.values() if a})
        ids = [mid for a, mid in senders.values() if a in known and mid]
        stored = {r["message_id"] for r in db.table("email_replies").select("message_id").in_("message_id", ids).execute().data} if ids else set()
        recorded = 0
        for uid, (address, message_id) in senders.items():
            if address not in known or (message_id and message_id in stored):
                continue
            _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            reply = parse(raw, now) if raw else None
            if reply and record(db, reply):
                recorded += 1
        if recorded:
            print(f"[replies] recorded {recorded} new repl{'y' if recorded == 1 else 'ies'}", flush=True)
        return recorded
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def recent(user_id: str, limit: int = 50) -> list[dict]:
    """Newest replies with the lead each one came from, for the dashboard."""
    db = get_supabase()
    rows = db.table("email_replies").select("id, email_id, campaign_id, recipient, from_name, subject, snippet, received_at, read_at") \
        .eq("user_id", user_id).order("received_at", desc=True).limit(limit).execute().data
    emails = sorted({r["recipient"] for r in rows})
    leads = {l["email"]: l for l in db.table("leads").select("id, email, name, lifecycle_stage").eq("user_id", user_id)
             .in_("email", emails).execute().data} if emails else {}
    campaign_ids = sorted({r["campaign_id"] for r in rows if r["campaign_id"]})
    names = {c["id"]: c["name"] for c in db.table("campaigns").select("id, name").in_("id", campaign_ids).execute().data} if campaign_ids else {}
    for r in rows:
        lead = leads.get(r["recipient"], {})
        r.update(lead_id=lead.get("id"), lead_name=lead.get("name") or r["from_name"], stage=lead.get("lifecycle_stage"),
                 campaign_name=names.get(r["campaign_id"]), unread=not r.get("read_at"))
    return rows


def _fetch_body(message_id: str) -> str | None:
    """The full text of one reply, read again from the reply mailbox (replies stored before the full text was kept)."""
    if not is_configured() or not message_id or message_id.startswith("<no-id"):
        return None
    imap = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=30)
    try:
        imap.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("search", None, "HEADER", "Message-ID", f'"{message_id}"')
        uids = [t for t in (data[0] or b"").split() if t.isdigit()] if data else []
        if not uids:
            return None
        _, parts = imap.uid("fetch", uids[-1], "(BODY.PEEK[])")
        raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
        return full_text(_body_text(email.message_from_bytes(raw))) if raw else None
    except Exception as e:
        logger.warning("Couldn't read reply %s from the reply mailbox: %s", message_id, e)
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def open_reply(user_id: str, reply_id: str) -> dict | None:
    """One reply in full, like opening an email: its whole text, and it counts as read from now on."""
    db = get_supabase()
    rows = db.table("email_replies").select("id, email_id, campaign_id, recipient, from_name, subject, snippet, body, message_id, received_at, read_at") \
        .eq("id", reply_id).eq("user_id", user_id).limit(1).execute().data
    if not rows:
        return None
    r = rows[0]
    changes: dict = {}
    if not r.get("body"):
        body = _fetch_body(r["message_id"])
        if body:
            r["body"] = changes["body"] = body
    if not r.get("read_at"):
        r["read_at"] = changes["read_at"] = datetime.now(timezone.utc).isoformat()
    if changes:
        db.table("email_replies").update(changes).eq("id", reply_id).execute()
    lead = (db.table("leads").select("id, name, lifecycle_stage").eq("user_id", user_id).eq("email", r["recipient"]).limit(1).execute().data or [{}])[0]
    campaign = (db.table("campaigns").select("name").eq("id", r["campaign_id"]).limit(1).execute().data or [{}])[0] if r.get("campaign_id") else {}
    r.pop("message_id", None)
    r.update(body=r.get("body") or r.get("snippet") or "", lead_id=lead.get("id"), lead_name=lead.get("name") or r["from_name"],
             stage=lead.get("lifecycle_stage"), campaign_name=campaign.get("name"), unread=False)
    return r


def mark_unread(user_id: str, reply_id: str) -> bool:
    return bool(get_supabase().table("email_replies").update({"read_at": None}).eq("id", reply_id).eq("user_id", user_id).execute().data)
