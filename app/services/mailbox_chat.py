"""Chat between the team's warm-up mailboxes, from the dashboard: one to one, or in a group.

Nobody has to open Hostinger Mail. Each message is a real email sent over the sender's own SMTP, to one mailbox
(a private chat) or to every other member (a group), so it counts for the warm-up like any other mail. It carries the
X-Warmup-Id header, and the warm-up inbox check (mailbox_warmup.check_inbox) finds it in each recipient's Inbox or
Spam, marks it read, rescues it from spam and records where it landed (chat_receipts).

A private chat also shows the automatic warm-up emails between the two mailboxes, so the whole conversation is in one
place. A group message can be answered privately: that opens the private chat with its sender, quoting it.
"""
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.services import mailbox_warmup as mw
from app.services import warmup_content

MAX_BODY = 5000
MAX_MESSAGES = 300
SUBJECT_LENGTH = 60
GROUP_MIN = 3  # with two mailboxes a group is the same as their private chat


def _db():
    return mw._db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pair_key(a: str, b: str) -> str:
    return ":".join(sorted((a, b)))


def name_of(mb: dict) -> str:
    return mb.get("display_name") or warmup_content.first_name(None, mb["email"])


def _public(mb: dict) -> dict:
    return {"id": mb["id"], "email": mb["email"], "name": name_of(mb), "status": mb["status"]}


def _boxes(user_id: str) -> dict[str, dict]:
    return {m["id"]: m for m in mw.list_mailboxes(user_id)}


def _members(thread_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {t: [] for t in thread_ids}
    if thread_ids:
        for r in _db().table("chat_members").select("thread_id, mailbox_id").in_("thread_id", thread_ids).execute().data:
            out.setdefault(r["thread_id"], []).append(r["mailbox_id"])
    return out


def first_subject(body: str, title: str | None = None) -> str:
    """A new conversation's subject: the group's name, else the first line of the first message."""
    if title:
        return title
    line = next((l.strip() for l in body.splitlines() if l.strip()), "Quick note")
    return line if len(line) <= SUBJECT_LENGTH else line[:SUBJECT_LENGTH - 1].rstrip() + "…"


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


# ---------- Conversations ----------

def list_threads(user_id: str, mailbox_id: str) -> dict:
    """What `mailbox_id` can chat in: its groups, and a private chat with each other mailbox (opened or not)."""
    boxes = _boxes(user_id)
    if mailbox_id not in boxes:
        raise LookupError("Mailbox not found.")
    db = _db()
    mine = [r["thread_id"] for r in db.table("chat_members").select("thread_id").eq("mailbox_id", mailbox_id).execute().data]
    threads = db.table("chat_threads").select("*").eq("user_id", user_id).in_("id", mine).execute().data if mine else []
    members = _members([t["id"] for t in threads])
    last: dict[str, dict] = {}
    if threads:
        rows = (db.table("chat_messages").select("thread_id, from_mailbox_id, body, created_at")
                .in_("thread_id", [t["id"] for t in threads]).order("created_at", desc=True).limit(1000).execute().data)
        for r in rows:
            last.setdefault(r["thread_id"], r)
    # The latest automatic warm-up email with each other mailbox, so a private chat shows activity before anyone wrote
    auto: dict[str, dict] = {}
    for r in (db.table("warmup_messages").select("from_mailbox_id, to_mailbox_id, subject, sent_at")
              .eq("user_id", user_id).eq("status", "sent").or_(f"from_mailbox_id.eq.{mailbox_id},to_mailbox_id.eq.{mailbox_id}")
              .order("sent_at", desc=True).limit(500).execute().data):
        other = r["to_mailbox_id"] if r["from_mailbox_id"] == mailbox_id else r["from_mailbox_id"]
        auto.setdefault(other, r)

    def preview(thread_id: str | None, other: str | None = None) -> dict | None:
        options = []
        if thread_id and thread_id in last:
            m = last[thread_id]
            options.append({"text": m["body"], "at": m["created_at"], "from_mailbox_id": m["from_mailbox_id"], "auto": False})
        if other and other in auto:
            a = auto[other]
            options.append({"text": a["subject"], "at": a["sent_at"], "from_mailbox_id": a["from_mailbox_id"], "auto": True})
        return max(options, key=lambda o: o["at"] or "") if options else None

    groups, direct_by_other = [], {}
    for t in threads:
        ids = [i for i in members.get(t["id"], []) if i in boxes]
        if t["kind"] == "group":
            groups.append({"id": t["id"], "kind": "group", "title": t["title"], "members": [_public(boxes[i]) for i in ids],
                           "last": preview(t["id"]), "updated_at": t["updated_at"]})
        else:
            other = next((i for i in ids if i != mailbox_id), None)
            if other:
                direct_by_other[other] = t["id"]
    direct = []
    for other_id, other in boxes.items():
        if other_id == mailbox_id:
            continue
        tid = direct_by_other.get(other_id)
        direct.append({"id": tid, "kind": "direct", "with": _public(other), "members": [_public(boxes[mailbox_id]), _public(other)],
                       "last": preview(tid, other_id)})
    sort_key = lambda t: (t["last"] or {}).get("at") or ""  # noqa: E731
    return {"groups": sorted(groups, key=sort_key, reverse=True), "direct": sorted(direct, key=sort_key, reverse=True)}


def open_direct(user_id: str, a: str, b: str) -> dict:
    """The private chat between two mailboxes, created the first time it is opened."""
    boxes = _boxes(user_id)
    if a == b or a not in boxes or b not in boxes:
        raise ValueError("Pick two different mailboxes of yours.")
    db = _db()
    key = pair_key(a, b)
    rows = db.table("chat_threads").select("*").eq("user_id", user_id).eq("pair_key", key).limit(1).execute().data
    if rows:
        return rows[0]
    thread = db.table("chat_threads").insert({"user_id": user_id, "kind": "direct", "pair_key": key}).execute().data[0]
    db.table("chat_members").insert([{"thread_id": thread["id"], "mailbox_id": a}, {"thread_id": thread["id"], "mailbox_id": b}]).execute()
    return thread


def create_group(user_id: str, title: str, mailbox_ids: list[str]) -> dict:
    boxes = _boxes(user_id)
    ids = list(dict.fromkeys(i for i in mailbox_ids if i in boxes))
    title = (title or "").strip()
    if not title:
        raise ValueError("Give the group a name.")
    if len(ids) < GROUP_MIN:
        raise ValueError(f"A group needs at least {GROUP_MIN} mailboxes; with two, use their private chat.")
    db = _db()
    thread = db.table("chat_threads").insert({"user_id": user_id, "kind": "group", "title": title[:80]}).execute().data[0]
    db.table("chat_members").insert([{"thread_id": thread["id"], "mailbox_id": i} for i in ids]).execute()
    return thread


def _thread(user_id: str, thread_id: str) -> tuple[dict, list[str], dict[str, dict]]:
    db = _db()
    rows = db.table("chat_threads").select("*").eq("user_id", user_id).eq("id", thread_id).limit(1).execute().data
    if not rows:
        raise LookupError("Conversation not found.")
    boxes = _boxes(user_id)
    return rows[0], [i for i in _members([thread_id])[thread_id] if i in boxes], boxes


def messages(user_id: str, thread_id: str) -> dict:
    """The conversation, oldest first: chat messages with where each landed, plus (private chats) the automatic
    warm-up emails between the two mailboxes."""
    thread, ids, boxes = _thread(user_id, thread_id)
    db = _db()
    rows = (db.table("chat_messages").select("id, from_mailbox_id, subject, body, quote, created_at")
            .eq("thread_id", thread_id).order("created_at", desc=True).limit(MAX_MESSAGES).execute().data)
    receipts: dict[str, list[dict]] = {}
    if rows:
        for r in db.table("chat_receipts").select("message_id, mailbox_id, landed_in, rescued, found_at").in_("message_id", [m["id"] for m in rows]).execute().data:
            receipts.setdefault(r["message_id"], []).append({k: r[k] for k in ("mailbox_id", "landed_in", "rescued", "found_at")})
    items = [{"id": m["id"], "source": "chat", "from_mailbox_id": m["from_mailbox_id"], "subject": m["subject"], "body": m["body"],
              "quote": m["quote"], "at": m["created_at"], "receipts": receipts.get(m["id"], [])} for m in rows]
    if thread["kind"] == "direct" and len(ids) == 2:
        a, b = ids
        for w in (db.table("warmup_messages").select("id, from_mailbox_id, to_mailbox_id, kind, subject, body, sent_at, landed_in, rescued, found_at")
                  .eq("user_id", user_id).eq("status", "sent")
                  .or_(f"and(from_mailbox_id.eq.{a},to_mailbox_id.eq.{b}),and(from_mailbox_id.eq.{b},to_mailbox_id.eq.{a})")
                  .order("sent_at", desc=True).limit(100).execute().data):
            items.append({"id": w["id"], "source": "auto", "from_mailbox_id": w["from_mailbox_id"], "subject": w["subject"], "body": w["body"],
                          "quote": None, "at": w["sent_at"],
                          "receipts": [{"mailbox_id": w["to_mailbox_id"], "landed_in": w["landed_in"], "rescued": w["rescued"], "found_at": w["found_at"]}]})
    items.sort(key=lambda i: i["at"] or "")
    return {
        "thread": {"id": thread["id"], "kind": thread["kind"], "title": thread.get("title"),
                   "members": [_public(boxes[i]) for i in ids]},
        "messages": items[-MAX_MESSAGES:],
    }


# ---------- Sending ----------

def build_email(row: dict, sender: dict, recipients: list[dict], parent: dict | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender.get("display_name") or "", sender["email"]))
    msg["To"] = ", ".join(formataddr((r.get("display_name") or "", r["email"])) for r in recipients)
    msg["Subject"] = row["subject"]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = row["message_id"]
    msg[mw.HEADER] = row["id"]
    if parent and parent.get("message_id"):
        msg["In-Reply-To"] = parent["message_id"]
        msg["References"] = parent["message_id"]
    body = row["body"]
    if row.get("quote"):
        body += "\n\n" + "\n".join("> " + line for line in row["quote"].splitlines())
    msg.set_content(body)
    return msg


def send(user_id: str, thread_id: str, from_mailbox_id: str, body: str, quote: str | None = None) -> dict:
    """Sends a message as `from_mailbox_id` to the conversation's other members, now. Raises ValueError with a
    readable message if it can't be sent (nothing is saved then)."""
    body = (body or "").strip()
    if not body:
        raise ValueError("Write a message first.")
    if len(body) > MAX_BODY:
        raise ValueError(f"A message can be at most {MAX_BODY} characters.")
    thread, ids, _ = _thread(user_id, thread_id)
    if from_mailbox_id not in ids:
        raise ValueError("That mailbox isn't in this conversation.")
    db = _db()
    full = {m["id"]: m for m in db.table("mailboxes").select(mw.PUBLIC_FIELDS + ", password_enc").eq("user_id", user_id).in_("id", ids).execute().data}
    sender = full[from_mailbox_id]
    if sender["status"] == "error":
        raise ValueError(f"{sender['email']} can't log in. Fix its login on the mailbox list first.")
    recipients = [full[i] for i in ids if i != from_mailbox_id and i in full]
    if not recipients:
        raise ValueError("No one else is in this conversation.")

    prior = (db.table("chat_messages").select("subject, message_id").eq("thread_id", thread_id)
             .order("created_at", desc=True).limit(1).execute().data)
    parent = prior[0] if prior else None
    row = {
        "id": str(uuid.uuid4()), "user_id": user_id, "thread_id": thread_id, "from_mailbox_id": from_mailbox_id,
        "subject": reply_subject(parent["subject"]) if parent else first_subject(body, thread.get("title")),
        "body": body, "quote": (quote or "").strip()[:MAX_BODY] or None,
        "message_id": make_msgid(idstring=uuid.uuid4().hex[:12], domain=sender["email"].split("@")[1]),
    }
    server = None
    try:
        server = mw._smtp(sender, mw.decrypt(sender["password_enc"]))
        server.send_message(build_email(row, sender, recipients, parent))
    except mw.MailboxError as e:
        raise ValueError(str(e))
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"The email couldn't be sent: {e}")
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
    saved = db.table("chat_messages").insert(row).execute().data[0]
    db.table("chat_receipts").insert([{"message_id": row["id"], "mailbox_id": r["id"]} for r in recipients]).execute()
    db.table("chat_threads").update({"updated_at": _now()}).eq("id", thread_id).execute()
    return {"id": saved["id"], "source": "chat", "from_mailbox_id": from_mailbox_id, "subject": saved["subject"], "body": saved["body"],
            "quote": saved.get("quote"), "at": saved.get("created_at") or _now(),
            "receipts": [{"mailbox_id": r["id"], "landed_in": None, "rescued": False, "found_at": None} for r in recipients]}


# ---------- Inbox check (called from mailbox_warmup.check_inbox) ----------

def pending_receipts(message_ids: list[str], mailbox_id: str) -> list[dict]:
    """The chat messages among `message_ids` that reached `mailbox_id` and weren't seen there yet."""
    if not message_ids:
        return []
    return (_db().table("chat_receipts").select("message_id").in_("message_id", message_ids)
            .eq("mailbox_id", mailbox_id).is_("found_at", "null").execute().data)


def record_receipt(message_id: str, mailbox_id: str, landed: str, rescued: bool, now: datetime):
    _db().table("chat_receipts").update({"landed_in": landed, "rescued": rescued, "found_at": now.isoformat()}) \
        .eq("message_id", message_id).eq("mailbox_id", mailbox_id).execute()
