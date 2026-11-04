"""Dashboard chat between the warm-up mailboxes, and newsletter handling in the inbox check."""
import random

from app.services import mailbox_chat as chat
from app.services import mailbox_warmup as mw
from app.services import newsletters as nl
from tests.test_mailbox_warmup import NOW, FakeDB, FakeIMAP, FakeSMTP, FakeTable, mailbox


class FillingTable(FakeTable):
    """Like the database: inserted rows get an id and timestamps."""

    def insert(self, rows):
        rows = rows if isinstance(rows, list) else [rows]
        for i, r in enumerate(rows):
            r.setdefault("id", f"{self.name}-{len(self.db.get(self.name, [])) + i}")
            r.setdefault("created_at", NOW.isoformat())
            r.setdefault("updated_at", NOW.isoformat())
        return super().insert(rows)


class FillingDB(FakeDB):
    def table(self, name):
        return FillingTable(self.tables, name)


def _setup(monkeypatch, n=3):
    boxes = [mailbox(i, password_enc=mw.encrypt("pw"), display_name=f"Person{i}", created_at=str(i)) for i in range(1, n + 1)]
    tables = {"mailboxes": [dict(b) for b in boxes], "chat_threads": [], "chat_members": [], "chat_messages": [],
              "chat_receipts": [], "warmup_messages": []}
    monkeypatch.setattr(mw, "_db", lambda: FillingDB(tables))
    server = FakeSMTP()
    monkeypatch.setattr(mw, "_smtp", lambda mb, pw: server)
    return boxes, tables, server


def test_subjects():
    assert chat.first_subject("\n  Can we meet tomorrow?\nThanks") == "Can we meet tomorrow?"
    long = chat.first_subject("x" * 100)
    assert long.endswith("…") and len(long) == chat.SUBJECT_LENGTH
    assert chat.first_subject("hi", "Design team") == "Design team"
    assert chat.reply_subject("Notes") == "Re: Notes" and chat.reply_subject("Re: Notes") == "Re: Notes"


def test_a_private_chat_is_a_real_threaded_email(monkeypatch):
    _, _, server = _setup(monkeypatch)
    thread = chat.open_direct("u1", "mb1", "mb2")
    assert chat.open_direct("u1", "mb2", "mb1")["id"] == thread["id"]  # one chat per pair

    first = chat.send("u1", thread["id"], "mb1", "Can we meet tomorrow?")
    second = chat.send("u1", thread["id"], "mb2", "Yes, 11 works")
    one, two = server.sent
    assert one["To"] == "Person2 <user2@arvistry.com>" and one["Subject"] == "Can we meet tomorrow?"
    assert one[mw.HEADER] == first["id"]
    assert two["In-Reply-To"] == one["Message-ID"] and two["Subject"] == "Re: Can we meet tomorrow?"
    assert [r["mailbox_id"] for r in first["receipts"]] == ["mb2"] and [r["mailbox_id"] for r in second["receipts"]] == ["mb1"]


def test_a_group_message_goes_to_everyone_else_and_can_be_answered_privately(monkeypatch):
    _, _, server = _setup(monkeypatch)
    group = chat.create_group("u1", "Design team", ["mb1", "mb2", "mb3"])
    msg = chat.send("u1", group["id"], "mb1", "Drawings are in the folder")
    assert server.sent[0]["To"] == "Person2 <user2@arvistry.com>, Person3 <user3@arvistry.com>"
    assert server.sent[0]["Subject"] == "Design team"
    assert sorted(r["mailbox_id"] for r in msg["receipts"]) == ["mb2", "mb3"]

    private = chat.open_direct("u1", "mb2", "mb1")
    chat.send("u1", private["id"], "mb2", "Thanks, which ones?", quote="Drawings are in the folder")
    assert "> Drawings are in the folder" in server.sent[1].get_content()
    assert server.sent[1]["To"] == "Person1 <user1@arvistry.com>"


def test_a_group_needs_three_mailboxes(monkeypatch):
    _setup(monkeypatch)
    try:
        chat.create_group("u1", "Two of us", ["mb1", "mb2"])
    except ValueError:
        return
    raise AssertionError("a two-mailbox group should be refused")


def test_only_members_can_send_and_empty_messages_are_refused(monkeypatch):
    _setup(monkeypatch)
    thread = chat.open_direct("u1", "mb1", "mb2")
    for sender, body in (("mb3", "hi"), ("mb1", "   ")):
        try:
            chat.send("u1", thread["id"], sender, body)
        except ValueError:
            continue
        raise AssertionError(f"{sender} {body!r} should be refused")


def test_the_inbox_check_reads_and_records_chat_messages(monkeypatch):
    boxes, tables, _ = _setup(monkeypatch, n=2)
    ids = ["33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"]
    tables["chat_receipts"] = [{"message_id": i, "mailbox_id": "mb2", "found_at": None} for i in ids]
    imap = FakeIMAP(ids)  # one in the inbox, one in spam
    monkeypatch.setattr(mw, "_imap", lambda mb, pw: imap)
    monkeypatch.setattr(nl, "process", lambda *a: {})

    result = mw.check_inbox(boxes[1], boxes, NOW, random.Random(1))

    assert result["chat"] == 2
    inbox, spam = tables["chat_receipts"]
    assert inbox["landed_in"] == "inbox" and not inbox["rescued"]
    assert spam["landed_in"] == "spam" and spam["rescued"] and ('"INBOX.Spam"', b"7", "INBOX") in imap.moved


# ---------- Newsletters ----------

def test_newsletter_domains_and_confirm_links():
    assert nl.clean_domains("Morningbrew.com, https://www.news.x.com/a ; bad") == ["morningbrew.com", "news.x.com"]
    assert nl.domain_matches("mail.morningbrew.com", ["morningbrew.com"])
    assert not nl.domain_matches("evilmorningbrew.com", ["morningbrew.com"])
    text = ("Unsubscribe: https://morningbrew.com/unsubscribe?u=1 \n"
            "Other: https://tracker.example.com/confirm?x=1 \n"
            "Please confirm: https://morningbrew.com/subscribe/confirm?t=abc.")
    assert nl.confirm_link(text, ["morningbrew.com"]) == "https://morningbrew.com/subscribe/confirm?t=abc"
    assert nl.confirm_link("https://x.list-manage.com/subscribe/confirm?u=1", ["dezeen.com"]).startswith("https://x.list-manage.com")
    assert nl.confirm_link("https://example.com/confirm", ["dezeen.com"]) is None
    assert not nl.is_public_url("http://127.0.0.1/confirm") and not nl.is_public_url("file:///etc/passwd")


class NewsIMAP:
    """Inbox: a confirmation email (uid 1). Spam: an issue (uid 7). Both from morningbrew.com."""
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self):
        confirm = b"From: Morning Brew <crew@morningbrew.com>\r\nSubject: Confirm your subscription\r\n\r\nClick https://morningbrew.com/confirm?t=1\r\n"
        issue = b"From: Morning Brew <crew@morningbrew.com>\r\nSubject: Today in business\r\n\r\nNews.\r\n"
        self.folders = {'"INBOX"': {b"1": confirm}, '"INBOX.Spam"': {b"7": issue}}
        self.selected, self.flags, self.moved = None, [], []

    def select(self, folder):
        self.selected = folder
        return "OK", [b"1"]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "search":
            return "OK", [b" ".join(box)]
        if command == "fetch":
            return "OK", [(b"1 (UID %s BODY[] {9}" % args[0], box[args[0]])]
        if command == "store":
            self.flags.append((self.selected, args[0], args[2]))
        if command == "move":
            self.moved.append((self.selected, args[0]))
        return "OK", [b""]


def test_newsletters_are_confirmed_read_and_rescued(monkeypatch):
    sub = {"id": "s1", "mailbox_id": "mb1", "name": "Morning Brew", "sender_domains": "morningbrew.com", "status": "pending",
           "received_count": 0, "rescued_count": 0}
    tables = {"newsletter_subscriptions": [sub]}
    monkeypatch.setattr(mw, "_db", lambda: FakeDB(tables))
    opened = []
    monkeypatch.setattr(nl, "open_link", lambda url: opened.append(url) or True)
    imap = NewsIMAP()

    result = nl.process(imap, {"id": "mb1"}, "INBOX.Spam", NOW, mw._move_to_inbox, mw._quote)

    assert opened == ["https://morningbrew.com/confirm?t=1"]
    assert result == {"received": 2, "rescued": 1, "confirmed": 1}
    assert sub["status"] == "confirmed" and sub["received_count"] == 2 and sub["rescued_count"] == 1
    assert ('"INBOX"', b"1", "(\\Seen)") in imap.flags and ('"INBOX.Spam"', b"7") in imap.moved
