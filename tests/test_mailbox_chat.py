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
    assert result == {"received": 2, "rescued": 1, "confirmed": 1, "found": 0}
    assert sub["status"] == "confirmed" and sub["received_count"] == 2 and sub["rescued_count"] == 1
    assert ('"INBOX"', b"1", "(\\Seen)") in imap.flags and ('"INBOX.Spam"', b"7") in imap.moved


class ReadIMAP:
    """Inbox: a Dezeen issue (uid 5) and someone else's email (uid 6)."""

    def __init__(self):
        self.mail = {
            b"5": b"From: Dezeen <mail@dezeen.com>\r\nSubject: =?utf-8?q?Today=E2=80=99s_news?=\r\nDate: Wed, 04 Nov 2026 19:03:16 +0000\r\n"
                  b"Content-Type: text/html\r\n\r\n<p>Hello <a href='https://dezeen.com'>read</a></p>\r\n",
            b"6": b"From: Client <boss@client.com>\r\nSubject: Private\r\n\r\nsecret\r\n",
        }

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"5" if "dezeen.com" in args[-1] else b""]
        if command == "fetch":
            uids = args[0].split(b",")
            return "OK", [(b"1 (UID %s FLAGS (\Seen) BODY[] {9}" % u, self.mail[u]) for u in uids]
        return "OK", [b""]


def test_a_newsletter_can_be_read_in_the_dashboard_but_no_other_mail(monkeypatch):
    sub = {"id": "s1", "user_id": "u1", "mailbox_id": "mb1", "sender_domains": "dezeen.com"}
    monkeypatch.setattr(mw, "_db", lambda: FakeDB({"newsletter_subscriptions": [sub]}))
    monkeypatch.setattr(mw, "get_mailbox", lambda u, m, with_password=False: {"id": m, "password_enc": mw.encrypt("pw")})
    monkeypatch.setattr(nl, "_open_inbox", lambda mb: ReadIMAP())

    listed = nl.list_emails("u1", "s1")
    assert [(e["uid"], e["subject"], e["seen"]) for e in listed] == [("5", "Today’s news", True)]
    one = nl.read_email("u1", "s1", "5")
    assert one["html"].startswith('<base target="_blank">') and "Hello" in one["html"]
    for uid in ("6", "1 OR 2"):
        try:
            nl.read_email("u1", "s1", uid)
        except LookupError:
            continue
        raise AssertionError(f"uid {uid} must not be readable")



def test_newsletters_are_found_by_themselves(monkeypatch):
    from datetime import datetime, timezone
    from app.services import mailbox_warmup as mw, newsletters as nl

    heads = {
        b"1": "From: Dezeen Daily <news@mail.dezeen.com>\r\nList-Unsubscribe: <https://dezeen.com/u>\r\n\r\n",
        b"2": "From: Ali <ali@gmail.com>\r\n\r\n",                                                    # a person: not a newsletter
        b"3": "From: Arvistry <resend@arvistry.com>\r\nList-Unsubscribe: <x>\r\n\r\n",                 # own mailbox domain
        b"4": "From: Ameer <a@hmdesigngroup.com>\r\nX-Warmup-Id: 1\r\nList-Id: <w>\r\n\r\n",           # warm-up email
        b"5": "From: Morning Brew <crew@morningbrew.com>\r\nList-Id: <brew.morningbrew.com>\r\n\r\n",
        b"6": "From: Dezeen Jobs <jobs@dezeen.com>\r\nList-Unsubscribe: <y>\r\n\r\n",                  # same sender as 1
    }

    class Imap:
        def select(self, folder):
            return "OK", [b"6"]
        def uid(self, cmd, *args):
            if cmd == "search":
                return "OK", [b" ".join(heads)]
            return "OK", [(b"%s (UID %s BODY[HEADER] {9}" % (u, u), heads[u].encode()) for u in args[0].split(b",")]

    tables = {"newsletter_subscriptions": [{"id": "old", "mailbox_id": "mb1", "sender_domains": "tldr.tech"}],
              "mailboxes": [{"email": "resend@arvistry.com", "user_id": "u1"}, {"email": "a@hmdesigngroup.com", "user_id": "u1"}]}

    class Q:
        def __init__(self, name):
            self.name, self.filters, self.row = name, [], None
        def select(self, *_):
            return self
        def eq(self, k, v):
            self.filters.append((k, v)); return self
        def insert(self, row):
            self.row = row; return self
        def execute(self):
            if self.row is not None:
                tables[self.name].append(self.row)
                return type("R", (), {"data": [self.row]})
            rows = [r for r in tables[self.name] if all(r.get(k) == v for k, v in self.filters)]
            return type("R", (), {"data": rows})

    monkeypatch.setattr(mw, "_db", lambda: type("DB", (), {"table": lambda self, n: Q(n)})())
    mb = {"id": "mb1", "user_id": "u1", "email": "a@hmdesigngroup.com"}
    assert nl.discover(Imap(), mb, datetime(2026, 11, 9, tzinfo=timezone.utc)) == 2
    added = {(r["name"], r["sender_domains"]) for r in tables["newsletter_subscriptions"] if r.get("user_id") == "u1"}
    assert added == {("Dezeen Jobs", "dezeen.com"), ("Morning Brew", "morningbrew.com")}  # named after its newest email
    assert nl.discover(Imap(), mb, datetime(2026, 11, 9, tzinfo=timezone.utc)) == 0   # each sender once
    assert nl.base_domain("mail.example.co.uk") == "example.co.uk"
