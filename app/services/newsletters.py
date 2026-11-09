"""Newsletters for the warm-up mailboxes: ordinary outside mail that a real inbox receives and reads.

Subscribing is done by a person (the dashboard lists well-known newsletters with their signup pages): signup forms
are protected against bots, and providers distrust addresses that robots sign up. Once a mailbox is marked as
subscribed, the warm-up inbox check (mailbox_warmup.check_inbox) handles that newsletter's mail by itself:

- the confirmation email ("please confirm your subscription"): its confirm link is opened, once
- each newsletter: moved out of Spam if it landed there, and marked read
- counts per subscription: received, rescued, last received

Only mail from the sender domains of a subscription is touched; other mail in the mailbox is left alone.
"""
import email
import ipaddress
import logging
import re
import socket
import time
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parseaddr
from html import unescape
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 3
MAX_PER_CHECK = 30
CONFIRM_WORDS = re.compile(r"confirm|verify|verif|activate|opt[- ]?in|validate|complete your (sign ?up|subscription)", re.I)
URL_RE = re.compile(r"""https?://[^\s"'<>)\]]+""", re.I)
# Services that send newsletters on other sites' behalf: their confirm links may sit on these domains
ESP_DOMAINS = ("list-manage.com", "mailchimp.com", "substack.com", "beehiiv.com", "convertkit.com", "kit.com", "ck.page",
               "mailerlite.com", "createsend.com", "hubspot.com", "hs-sites.com", "klaviyo.com", "sendgrid.net", "sendinblue.com",
               "brevo.com", "mailjet.com", "campaign-archive.com", "emailoctopus.com", "buttondown.email", "ghost.io")

# Well-known newsletters, mostly design, architecture and business, so the mail suits HM Design Group's mailboxes.
# `domains` are the usual sender domains; they can be changed when a subscription is saved.
CATALOG = [
    {"name": "ArchDaily", "topic": "Architecture", "url": "https://www.archdaily.com", "domains": ["archdaily.com"]},
    {"name": "Dezeen", "topic": "Architecture & design", "url": "https://www.dezeen.com/newsletter/", "domains": ["dezeen.com"]},
    {"name": "Architectural Digest", "topic": "Interiors", "url": "https://www.architecturaldigest.com/newsletter", "domains": ["architecturaldigest.com", "condenast.com"]},
    {"name": "Houzz", "topic": "Home design", "url": "https://www.houzz.com", "domains": ["houzz.com"]},
    {"name": "Morning Brew", "topic": "Business news", "url": "https://www.morningbrew.com/daily", "domains": ["morningbrew.com"]},
    {"name": "The Hustle", "topic": "Business", "url": "https://thehustle.co", "domains": ["thehustle.co", "hubspot.com"]},
    {"name": "TLDR", "topic": "Tech news", "url": "https://tldr.tech", "domains": ["tldrnewsletter.com", "tldr.tech"]},
    {"name": "Smashing Magazine", "topic": "Web design", "url": "https://www.smashingmagazine.com/the-smashing-newsletter/", "domains": ["smashingmagazine.com"]},
    {"name": "Product Hunt", "topic": "New products", "url": "https://www.producthunt.com/newsletters", "domains": ["producthunt.com"]},
    {"name": "Medium Daily Digest", "topic": "Articles", "url": "https://medium.com", "domains": ["medium.com"]},
    {"name": "Canva", "topic": "Design tips", "url": "https://www.canva.com", "domains": ["canva.com"]},
    {"name": "Notion", "topic": "Productivity", "url": "https://www.notion.so", "domains": ["notion.so", "notion.com", "makenotion.com"]},
]


def _db():
    from app.services import mailbox_warmup  # one place to reach the database (tests replace it there)

    return mailbox_warmup._db()


# ---------- Pure ----------

def clean_domains(value) -> list[str]:
    """'Morningbrew.com, https://news.x.com/a' -> ['morningbrew.com', 'news.x.com']"""
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", value or "")
    out = []
    for item in items:
        d = item.strip().lower()
        if "//" in d:
            d = urlparse(d).hostname or ""
        d = d.split("@")[-1].strip("./")
        if d.startswith("www."):
            d = d[4:]
        if "." in d and d not in out:
            out.append(d)
    return out


def domain_matches(host: str, domains: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def sender_domain(msg: Message) -> str:
    return parseaddr(msg.get("From", ""))[1].rpartition("@")[2].lower()


def is_confirmation(msg: Message, text: str) -> bool:
    return bool(CONFIRM_WORDS.search(str(msg.get("Subject", "")))) or bool(CONFIRM_WORDS.search(text[:3000]))


def body_text(msg: Message) -> str:
    """Plain text and HTML parts together (links are looked for in both)."""
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                continue
    return unescape("\n".join(parts))


def confirm_link(text: str, domains: list[str]) -> str | None:
    """The 'confirm my subscription' link: on the newsletter's own domain or its mailing service, never an
    unsubscribe link."""
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;")
        low = url.lower()
        if "unsub" in low or "optout" in low or "opt-out" in low:
            continue
        host = urlparse(url).hostname or ""
        if not (domain_matches(host, domains) or domain_matches(host, list(ESP_DOMAINS))):
            continue
        if CONFIRM_WORDS.search(low) or "subscribe" in low or "subscription" in low:
            return url
    return None


def is_public_url(url: str) -> bool:
    """Only public internet addresses are opened (never the server's own network)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def open_link(url: str) -> bool:
    if not is_public_url(url):
        return False
    try:
        r = httpx.get(url, follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; mail client)"})
        return r.status_code < 400
    except httpx.HTTPError as e:
        logger.warning("Couldn't open a newsletter confirm link: %s", e)
        return False


# ---------- Subscriptions (API) ----------

def catalog() -> list[dict]:
    return CATALOG


def list_subscriptions(user_id: str) -> list[dict]:
    return (_db().table("newsletter_subscriptions").select("*").eq("user_id", user_id).order("subscribed_at").execute().data)


def add_subscription(user_id: str, mailbox_id: str, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    domains = clean_domains(data.get("sender_domains") or "")
    if not name:
        raise ValueError("Give the newsletter a name.")
    if not domains:
        raise ValueError("Add the domain its emails come from, for example morningbrew.com.")
    db = _db()
    if db.table("newsletter_subscriptions").select("id").eq("mailbox_id", mailbox_id).eq("name", name).limit(1).execute().data:
        raise ValueError(f"This mailbox is already subscribed to {name}.")
    return db.table("newsletter_subscriptions").insert({
        "user_id": user_id, "mailbox_id": mailbox_id, "name": name[:120],
        "signup_url": (data.get("signup_url") or "").strip()[:500] or None, "sender_domains": ",".join(domains),
    }).execute().data[0]


def remove_subscription(user_id: str, sub_id: str) -> bool:
    return bool(_db().table("newsletter_subscriptions").delete().eq("id", sub_id).eq("user_id", user_id).execute().data)


# ---------- Inbox check ----------

DISCOVER_LIMIT = 60      # newest Inbox emails looked at for new newsletters, per check
MAX_AUTO_PER_MAILBOX = 40
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}  # example.co.uk -> keep three labels


def base_domain(host: str) -> str:
    """news.mail.dezeen.com -> dezeen.com; mail.example.co.uk -> example.co.uk."""
    labels = [x for x in host.lower().strip(".").split(".") if x]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_newsletter(head: Message) -> bool:
    """Bulk mail a person signed up for carries a List-Unsubscribe or List-Id header (Gmail's Unsubscribe link uses it)."""
    return bool(head.get("List-Unsubscribe") or head.get("List-Id")) and not head.get("X-Warmup-Id")


def discover(imap, mb: dict, now: datetime) -> int:
    """Newsletters that arrived in `mb`'s Inbox are added to its subscriptions by themselves: no list to pick from and
    nothing to click. Only the Inbox is looked at, so a sender is only ever taken out of spam (by process()) once it
    has reached the Inbox before; real spam stays in spam. Mail from the user's own mailboxes is never a newsletter."""
    if not mb.get("user_id"):
        return 0
    db = _db()
    subs = db.table("newsletter_subscriptions").select("id, sender_domains").eq("mailbox_id", mb["id"]).execute().data
    if len(subs) >= MAX_AUTO_PER_MAILBOX:
        return 0
    known = {d for s in subs for d in clean_domains(s["sender_domains"])}
    own = {base_domain(m["email"].rsplit("@", 1)[-1]) for m in
           db.table("mailboxes").select("email").eq("user_id", mb["user_id"]).execute().data}
    if imap.select('"INBOX"')[0] != "OK":
        return 0
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    _, data = imap.uid("search", None, "SINCE", since)
    uids = sorted((data[0] or b"").split() if data else [], key=int, reverse=True)[:DISCOVER_LIMIT]
    if not uids:
        return 0
    _, parts = imap.uid("fetch", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (FROM LIST-UNSUBSCRIBE LIST-ID X-WARMUP-ID)])")
    found: dict[str, str] = {}
    for part in parts or []:
        if not isinstance(part, tuple):
            continue
        head = email.message_from_bytes(part[1] or b"")
        host = sender_domain(head)
        if not host or not is_newsletter(head):
            continue
        dom = base_domain(host)
        if dom in own or dom in known or domain_matches(host, list(known)) or dom in found:
            continue
        name = parseaddr(_decoded(head.get("From")))[0].strip().strip('"') or dom
        found[dom] = name[:120]
    added = 0
    for dom, name in found.items():
        if len(subs) + added >= MAX_AUTO_PER_MAILBOX:
            break
        db.table("newsletter_subscriptions").insert({
            "user_id": mb["user_id"], "mailbox_id": mb["id"], "name": name, "signup_url": None, "sender_domains": dom,
        }).execute()
        added += 1
    if added:
        logger.info("Found %d new newsletter(s) in %s: %s", added, mb["email"], ", ".join(found))
    return added


def process(imap, mb: dict, junk: str | None, now: datetime, move_to_inbox, quote) -> dict:
    """Handles the newsletters that reached `mb`: finds new ones, rescues from spam, marks read, confirms pending ones.
    `move_to_inbox(imap, uid)` and `quote(folder)` come from mailbox_warmup."""
    db = _db()
    result = {"received": 0, "rescued": 0, "confirmed": 0, "found": 0}
    try:
        result["found"] = discover(imap, mb, now)
    except Exception as e:  # finding new ones is extra: the known newsletters are still handled
        logger.warning("Newsletter discovery failed for %s: %s", mb.get("email"), e)
    subs = db.table("newsletter_subscriptions").select("*").eq("mailbox_id", mb["id"]).execute().data
    if not subs:
        return result
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    for folder, landed in (("INBOX", "inbox"), (junk, "spam")):
        if not folder or imap.select(quote(folder))[0] != "OK":
            continue
        for sub in subs:
            domains = clean_domains(sub["sender_domains"])
            # Inbox: only mail not read yet (read mail was handled before, or by a person). Spam: all of it.
            criteria = ["SINCE", since] + (["UNSEEN"] if landed == "inbox" else [])
            uids: list[bytes] = []
            for d in domains:
                _, data = imap.uid("search", None, *criteria, "FROM", f'"{d}"')
                uids += [u for u in ((data[0] or b"").split() if data else []) if u not in uids]
            changes: dict = {}
            for uid in uids[:MAX_PER_CHECK]:
                _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                if not domain_matches(sender_domain(msg), domains):
                    continue  # FROM search is a substring match; check the real sender domain
                text = body_text(msg)
                if sub["status"] == "pending" and is_confirmation(msg, text):
                    link = confirm_link(text, domains)
                    if link and open_link(link):
                        changes.update(status="confirmed", confirmed_at=now.isoformat())
                        sub["status"] = "confirmed"
                        result["confirmed"] += 1
                elif sub["status"] == "pending":
                    # A regular issue arrived: the subscription is running (some newsletters need no confirmation)
                    changes.update(status="confirmed", confirmed_at=now.isoformat())
                    sub["status"] = "confirmed"
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
                if landed == "spam" and move_to_inbox(imap, uid):
                    changes["rescued_count"] = sub.get("rescued_count", 0) + 1
                    sub["rescued_count"] = changes["rescued_count"]
                    result["rescued"] += 1
                changes["received_count"] = sub.get("received_count", 0) + 1
                sub["received_count"] = changes["received_count"]
                changes["last_received_at"] = now.isoformat()
                result["received"] += 1
            if changes:
                db.table("newsletter_subscriptions").update(changes).eq("id", sub["id"]).execute()
    return result


# ---------- Reading the newsletters in the dashboard ----------

READ_DAYS = 30
READ_LIMIT = 30


def _decoded(value) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _iso(date_header: str | None) -> str | None:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(date_header).isoformat() if date_header else None
    except Exception:
        return None


def html_of(msg: Message) -> str:
    """The email as HTML for a sandboxed frame: its HTML part, else its text in a <pre>. Links open in a new tab."""
    html = text = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        kind = part.get_content_type()
        if kind not in ("text/html", "text/plain") or part.get_filename():
            continue
        try:
            body = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if kind == "text/html" and html is None:
            html = body
        elif kind == "text/plain" and text is None:
            text = body
    if html is None:
        from html import escape

        html = f"<pre style='white-space:pre-wrap;font:14px/1.5 sans-serif'>{escape(text or '')}</pre>"
    return '<base target="_blank">' + html


def _subscription(user_id: str, sub_id: str) -> tuple[dict, dict]:
    from app.services import mailbox_warmup as mw

    rows = _db().table("newsletter_subscriptions").select("*").eq("id", sub_id).eq("user_id", user_id).limit(1).execute().data
    if not rows:
        raise LookupError("Subscription not found.")
    mb = mw.get_mailbox(user_id, rows[0]["mailbox_id"], with_password=True)
    if not mb:
        raise LookupError("Mailbox not found.")
    return rows[0], mb


def _open_inbox(mb: dict):
    from app.services import mailbox_warmup as mw

    imap = mw._imap(mb, mw.decrypt(mb["password_enc"]))
    imap.select('"INBOX"', readonly=True)
    return imap


def list_emails(user_id: str, sub_id: str) -> list[dict]:
    """The newsletter's emails in the mailbox's Inbox (last 30 days, newest first). Only mail from its sender domains."""
    from app.services import mailbox_warmup as mw

    sub, mb = _subscription(user_id, sub_id)
    domains = clean_domains(sub["sender_domains"])
    since = (datetime.now().astimezone() - timedelta(days=READ_DAYS)).strftime("%d-%b-%Y")
    imap = _open_inbox(mb)
    try:
        uids: set[bytes] = set()
        for d in domains:
            _, data = imap.uid("search", None, "SINCE", since, "FROM", f'"{d}"')
            uids |= set((data[0] or b"").split()) if data else set()
        newest = sorted(uids, key=int, reverse=True)[:READ_LIMIT]
        if not newest:
            return []
        _, parts = imap.uid("fetch", b",".join(newest), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        out = []
        for part in parts or []:
            if not isinstance(part, tuple):
                continue
            uid = re.search(rb"UID (\d+)", part[0])
            head = email.message_from_bytes(part[1] or b"")
            if not uid or not domain_matches(sender_domain(head), domains):
                continue
            out.append({"uid": uid.group(1).decode(), "from": _decoded(head.get("From")), "subject": _decoded(head.get("Subject")) or "(no subject)",
                        "date": _iso(head.get("Date")), "seen": b"\Seen" in part[0]})
        return sorted(out, key=lambda e: int(e["uid"]), reverse=True)
    finally:
        mw._logout(imap)


FEED_SECONDS = 45   # the feed is read from the mailboxes at most this often per user
FEED_PER_NEWSLETTER = 10
FEED_LIMIT = 40
_feed_cache: dict[str, tuple[float, list[dict]]] = {}


def feed(user_id: str, fresh: bool = False) -> list[dict]:
    """The newest newsletter emails in the Inboxes of all the user's mailboxes, newest first: one list, like an inbox.
    One login per mailbox; cached briefly so the dashboard's refresh doesn't log in every few seconds."""
    from app.services import mailbox_warmup as mw

    hit = _feed_cache.get(user_id)
    if hit and not fresh and time.time() - hit[0] < FEED_SECONDS:
        return hit[1]
    by_mailbox: dict[str, list[dict]] = {}
    for s in list_subscriptions(user_id):
        by_mailbox.setdefault(s["mailbox_id"], []).append(s)
    since = (datetime.now().astimezone() - timedelta(days=READ_DAYS)).strftime("%d-%b-%Y")
    out: list[dict] = []
    for mailbox_id, subs in by_mailbox.items():
        mb = mw.get_mailbox(user_id, mailbox_id, with_password=True)
        if not mb or mb.get("status") == "error":
            continue
        try:
            imap = _open_inbox(mb)
        except Exception as e:  # one mailbox that can't log in must not empty the whole feed
            logger.warning("Newsletter feed: can't open %s: %s", mb["email"], e)
            continue
        try:
            for s in subs:
                domains = clean_domains(s["sender_domains"])
                uids: set[bytes] = set()
                for d in domains:
                    _, data = imap.uid("search", None, "SINCE", since, "FROM", f'"{d}"')
                    uids |= set((data[0] or b"").split()) if data else set()
                newest = sorted(uids, key=int, reverse=True)[:FEED_PER_NEWSLETTER]
                if not newest:
                    continue
                _, parts = imap.uid("fetch", b",".join(newest), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                for part in parts or []:
                    if not isinstance(part, tuple):
                        continue
                    uid = re.search(rb"UID (\d+)", part[0])
                    head = email.message_from_bytes(part[1] or b"")
                    if not uid or not domain_matches(sender_domain(head), domains):
                        continue
                    out.append({
                        "sub_id": s["id"], "newsletter": s["name"], "mailbox_id": mailbox_id, "mailbox": mb["email"],
                        "uid": uid.group(1).decode(), "from": _decoded(head.get("From")),
                        "subject": _decoded(head.get("Subject")) or "(no subject)", "date": _iso(head.get("Date")),
                        "seen": b"\\Seen" in part[0],
                    })
        except Exception as e:
            logger.warning("Newsletter feed: reading %s failed: %s", mb["email"], e)
        finally:
            mw._logout(imap)
    def when(e: dict) -> float:  # dates come in each sender's own timezone: compare them as moments
        try:
            return datetime.fromisoformat(e["date"]).timestamp() if e["date"] else 0.0
        except ValueError:
            return 0.0
    out.sort(key=when, reverse=True)
    out = out[:FEED_LIMIT]
    _feed_cache[user_id] = (time.time(), out)
    return out


def read_email(user_id: str, sub_id: str, uid: str) -> dict:
    from app.services import mailbox_warmup as mw

    if not uid.isdigit():
        raise LookupError("Email not found.")
    sub, mb = _subscription(user_id, sub_id)
    imap = _open_inbox(mb)
    try:
        _, parts = imap.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
    finally:
        mw._logout(imap)
    if not raw:
        raise LookupError("Email not found. It may have been deleted.")
    msg = email.message_from_bytes(raw)
    if not domain_matches(sender_domain(msg), clean_domains(sub["sender_domains"])):
        raise LookupError("Email not found.")  # only this newsletter's mail can be read here
    return {"uid": uid, "from": _decoded(msg.get("From")), "subject": _decoded(msg.get("Subject")) or "(no subject)",
            "date": _iso(msg.get("Date")), "html": html_of(msg)}
