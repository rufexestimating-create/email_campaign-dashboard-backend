"""Mailbox warm-up: the daily ramp, today's plan, finding the spam folder, and what the inbox bot does with each email."""
import random
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import mailbox_warmup as mw
from app.services import warmup_content

TZ = ZoneInfo("Asia/Karachi")
# 10:00 in Karachi
NOW = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)


def mailbox(i: int, started: str = "2026-11-03", limit: int = 30, **extra) -> dict:
    return {"id": f"mb{i}", "user_id": "u1", "email": f"user{i}@arvistry.com", "display_name": None,
            "warmup_started_on": started, "daily_limit": limit, "status": "active", **extra}


# ---------- Ramp and plan ----------

def test_the_ramp_starts_small_and_grows_to_the_daily_limit():
    assert [mw.daily_target(d, 30) for d in (1, 3, 4, 7, 8, 14, 15, 21, 22, 60)] == [3, 3, 6, 6, 12, 12, 20, 20, 30, 30]
    assert mw.daily_target(20, 10) == 10  # never above the mailbox's own limit
    assert mw.warmup_day(date(2026, 11, 1), date(2026, 11, 3)) == 3


def test_one_mailbox_has_no_one_to_write_to():
    assert mw.plan_day([mailbox(1)], {}, NOW, random.Random(1), TZ) == []


def test_plan_sends_to_other_mailboxes_inside_office_hours():
    boxes = [mailbox(1), mailbox(2), mailbox(3, started="2026-10-01")]
    sends = mw.plan_day(boxes, {}, NOW, random.Random(7), TZ)
    by_sender = {b["id"]: [s for s in sends if s["from"]["id"] == b["id"]] for b in boxes}
    # Planned at 10:00, so 8 of the 9 office hours are left
    assert len(by_sender["mb1"]) == 3 and len(by_sender["mb3"]) == 27
    for s in sends:
        assert s["to"]["id"] != s["from"]["id"]
        local = s["at"].astimezone(TZ)
        assert local.date() == date(2026, 11, 3) and 10 <= local.hour < 18
    assert sends == sorted(sends, key=lambda s: s["at"])


def test_a_planned_mailbox_is_not_planned_again_and_evenings_get_nothing():
    boxes = [mailbox(1), mailbox(2)]
    sends = mw.plan_day(boxes, {"mb1": 3}, NOW, random.Random(1), TZ)
    assert {s["from"]["id"] for s in sends} == {"mb2"}
    evening = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)  # 19:00 in Karachi
    assert mw.plan_day(boxes, {}, evening, random.Random(1), TZ) == []


# ---------- Content, passwords, message ----------

def test_emails_read_like_ordinary_mail():
    rng = random.Random(3)
    subject, body = warmup_content.compose_initial(rng, "Ali", "Sara")
    assert subject in warmup_content.SUBJECTS and "Sara" in body and body.rstrip().endswith("Ali")
    re_subject, reply = warmup_content.compose_reply(rng, subject, "Sara", "Ali")
    assert re_subject == f"Re: {subject}" and reply.rstrip().endswith("Sara")
    assert warmup_content.compose_reply(rng, "Re: Notes", "A", "B")[0] == "Re: Notes"
    assert warmup_content.first_name(None, "sara.ahmed@arvistry.com") == "Sara"
    assert warmup_content.first_name("Ali Khan", "x@y.com") == "Ali"


def test_passwords_are_stored_encrypted():
    token = mw.encrypt("example-Pass:123")
    assert "example-Pass:123" not in token and mw.decrypt(token) == "example-Pass:123"
    with pytest.raises(ValueError):
        mw.decrypt("not-a-token")


def test_each_email_carries_its_warmup_id_and_threads_replies():
    row = {"id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "subject": "Re: Notes", "body": "Thanks", "message_id": "<b@arvistry.com>"}
    msg = mw.build_message({"email": "a@arvistry.com", "display_name": "Ali"}, {"email": "b@arvistry.com"}, row, "<a@arvistry.com>")
    assert msg[mw.HEADER] == row["id"] and msg["In-Reply-To"] == "<a@arvistry.com>" and msg["From"] == "Ali <a@arvistry.com>"


# ---------- Spam folder ----------

@pytest.mark.parametrize("lines, expected", [
    ([b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren \\Junk) "." "INBOX.Spam"'], "INBOX.Spam"),          # Hostinger
    ([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"'], "[Gmail]/Spam"),      # Gmail
    ([b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren) "/" "Junk E-mail"'], "Junk E-mail"),                 # no \Junk flag
    ([b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren) "/" Sent'], None),
])
def test_finds_the_spam_folder(lines, expected):
    assert mw.spam_folder(lines) == expected


# ---------- Report ----------

def test_report_counts_where_each_mailboxs_emails_landed():
    boxes = [mailbox(1, started="2026-10-30"), mailbox(2)]
    sent = "2026-11-03T06:00:00+00:00"
    messages = [
        {"from_mailbox_id": "mb1", "to_mailbox_id": "mb2", "kind": "initial", "plan_date": "2026-11-03", "status": "sent",
         "sent_at": sent, "found_at": sent, "landed_in": "inbox", "rescued": False},
        {"from_mailbox_id": "mb1", "to_mailbox_id": "mb2", "kind": "initial", "plan_date": "2026-11-03", "status": "sent",
         "sent_at": sent, "found_at": sent, "landed_in": "spam", "rescued": True},
        {"from_mailbox_id": "mb1", "to_mailbox_id": "mb2", "kind": "initial", "plan_date": "2026-11-03", "status": "scheduled",
         "sent_at": None, "found_at": None, "landed_in": None, "rescued": False},
        {"from_mailbox_id": "mb2", "to_mailbox_id": "mb1", "kind": "reply", "plan_date": "2026-11-03", "status": "sent",
         "sent_at": sent, "found_at": None, "landed_in": None, "rescued": False},
    ]
    one, two = mw.summarize(boxes, messages, date(2026, 11, 3), TZ)
    assert one["warmup_day"] == 5 and one["target_today"] == 6
    assert (one["sent_today"], one["scheduled_today"], one["inbox_7d"], one["spam_7d"], one["inbox_rate_7d"]) == (2, 1, 1, 1, 0.5)
    assert one["series"][-1] == {"date": "2026-11-03", "sent": 2, "inbox": 1, "spam": 1}
    assert (two["received_today"], two["rescued_7d"], two["replies_7d"], two["waiting"]) == (2, 1, 1, 1)
    assert one["health"] == "starting"  # too few emails to judge yet


# ---------- Inbox bot ----------

class FakeTable:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op, self.payload = db, name, [], "select", None

    def select(self, *_a, **_k):
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows if isinstance(rows, list) else [rows]
        return self

    def update(self, changes):
        self.op, self.payload = "update", changes
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def is_(self, col, _null):
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def lte(self, col, val):
        self.filters.append(lambda r: r.get(col) <= val)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        rows = self.db.setdefault(self.name, [])
        if self.op == "insert":
            rows.extend(self.payload)
            return type("R", (), {"data": self.payload})
        hits = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
        return type("R", (), {"data": hits})


class FakeDB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeTable(self.tables, name)


class FakeIMAP:
    """Inbox has one warm-up email (uid 1), Spam has another (uid 7)."""
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self, ids):
        self.folders = {'"INBOX"': {b"1": ids[0]}, '"INBOX.Spam"': {b"7": ids[1]}}
        self.selected, self.flags, self.moved = None, [], []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren \\Junk) "." "INBOX.Spam"']

    def select(self, folder):
        self.selected = folder
        return "OK", [b"1"]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "search":
            return "OK", [b" ".join(box)]
        if command == "fetch":
            return "OK", [(b"%s (UID %s BODY[HEADER.FIELDS (X-WARMUP-ID)] {50}" % (u, u), f"X-Warmup-Id: {box[u]}\r\n\r\n".encode()) for u in box]
        if command == "store":
            self.flags.append((self.selected, args[0], args[2]))
        if command == "move":
            self.moved.append((self.selected, args[0], args[1]))
        return "OK", [b""]

    def expunge(self):
        return "OK", []

    def logout(self):
        pass


def test_inbox_bot_records_rescues_reads_and_answers(monkeypatch):
    ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    boxes = [mailbox(1, password_enc=mw.encrypt("pw")), mailbox(2, password_enc=mw.encrypt("pw"))]
    tables = {
        "mailboxes": [dict(b) for b in boxes],
        "warmup_messages": [
            {"id": i, "from_mailbox_id": "mb1", "to_mailbox_id": "mb2", "kind": "initial", "subject": "Notes from today", "found_at": None}
            for i in ids
        ],
    }
    imap = FakeIMAP(ids)
    monkeypatch.setattr(mw, "_db", lambda: FakeDB(tables))
    monkeypatch.setattr(mw, "_imap", lambda mb, pw: imap)
    monkeypatch.setattr(mw, "REPLY_RATE", 1.0)

    result = mw.check_inbox(boxes[1], boxes, NOW, random.Random(1))

    assert {k: result[k] for k in ("inbox", "spam", "replies")} == {"inbox": 1, "spam": 1, "replies": 2}
    inbox_row, spam_row = tables["warmup_messages"][:2]
    assert inbox_row["landed_in"] == "inbox" and not inbox_row["rescued"]
    assert spam_row["landed_in"] == "spam" and spam_row["rescued"]
    assert ('"INBOX.Spam"', b"7", "INBOX") in imap.moved
    assert ('"INBOX"', b"1", "(\\Seen)") in imap.flags and ('"INBOX.Spam"', b"7", "(\\Seen)") in imap.flags
    replies = tables["warmup_messages"][2:]
    assert all(r["kind"] == "reply" and r["from_mailbox_id"] == "mb2" and r["to_mailbox_id"] == "mb1" for r in replies)
    assert all(r["subject"] == "Re: Notes from today" for r in replies)

    # A second pass finds nothing new: each email is handled once
    again = mw.check_inbox(boxes[1], boxes, NOW, random.Random(1))
    assert {k: again[k] for k in ("inbox", "spam", "replies")} == {"inbox": 0, "spam": 0, "replies": 0}


# ---------- Replies in office hours, retries, sending ----------

def test_replies_wait_for_office_hours():
    rng = random.Random(1)
    at_noon = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)  # 12:00 Karachi
    assert mw.office_hours(at_noon, TZ, rng) == at_noon
    for late in (datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc), datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)):
        local = mw.office_hours(late, TZ, rng).astimezone(TZ)  # 20:30 -> next morning, 06:00 -> same morning
        assert mw.WINDOW[0] <= local.hour < mw.WINDOW[1] and local > late.astimezone(TZ)
    assert mw.office_hours(datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc), TZ, rng).astimezone(TZ).date() == date(2026, 11, 4)


def test_an_unreachable_server_is_retried_before_the_mailbox_is_stopped(monkeypatch):
    errors = []
    monkeypatch.setattr(mw, "_set_error", lambda mid, e: errors.append(mid))
    monkeypatch.setattr(mw, "_failures", {})
    blip = mw.MailboxError("timeout", transient=True)
    assert [mw._mailbox_failed("mb1", blip) for _ in range(mw.FAILS_BEFORE_ERROR)] == [False] * (mw.FAILS_BEFORE_ERROR - 1) + [True]
    assert errors == ["mb1"]
    assert mw._mailbox_failed("mb2", mw.MailboxError("wrong password")) is True  # a wrong password stops it at once


def _due(i, **extra):
    return {"id": f"m{i}", "from_mailbox_id": "mb1", "to_mailbox_id": "mb2", "kind": "initial", "subject": "Notes", "body": "Hi",
            "status": "scheduled", "scheduled_at": "2026-11-03T04:00:00+00:00", "parent_id": None, "error": None, **extra}


class FakeSMTP:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send_message(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)

    def quit(self):
        pass


def _send_setup(monkeypatch, rows, smtp):
    boxes = [mailbox(1, password_enc=mw.encrypt("pw")), mailbox(2, password_enc=mw.encrypt("pw"))]
    tables = {"warmup_messages": rows, "mailboxes": [dict(b) for b in boxes]}
    monkeypatch.setattr(mw, "_db", lambda: FakeDB(tables))
    monkeypatch.setattr(mw, "_failures", {})
    monkeypatch.setattr(mw, "_smtp", smtp)
    return boxes


def test_sending_marks_each_email_sent_once(monkeypatch):
    server = FakeSMTP()
    rows = [_due(1), _due(2, status="sent")]  # m2 was already claimed by another copy of the scheduler
    boxes = _send_setup(monkeypatch, rows, lambda mb, pw: server)
    assert mw.send_due(boxes, NOW) == 1 and len(server.sent) == 1
    assert rows[0]["status"] == "sent" and rows[0]["sent_at"] and rows[0]["message_id"]


def test_a_failed_send_is_tried_again_then_given_up(monkeypatch):
    rows = [_due(1)]
    boxes = _send_setup(monkeypatch, rows, lambda mb, pw: FakeSMTP(fail=RuntimeError("421 try later")))
    for attempt in range(1, mw.MAX_SEND_TRIES + 1):
        rows[0]["scheduled_at"] = "2026-11-03T04:00:00+00:00"
        mw.send_due(boxes, NOW)
        assert rows[0]["error"].startswith(f"[try {attempt}]")
    assert rows[0]["status"] == "failed"


def test_an_unreachable_server_postpones_instead_of_stopping(monkeypatch):
    rows = [_due(1), _due(2)]

    def down(mb, pw):
        raise mw.MailboxError("timeout", transient=True)

    boxes = _send_setup(monkeypatch, rows, down)
    assert mw.send_due(boxes, NOW) == 0
    assert all(r["status"] == "scheduled" and r["scheduled_at"] > NOW.isoformat() for r in rows)


def test_without_move_only_the_rescued_email_is_expunged():
    class OldIMAP:
        capabilities = ("IMAP4REV1", "UIDPLUS")

        def __init__(self):
            self.calls = []

        def uid(self, command, *args):
            self.calls.append((command, *args))
            return "OK", [b""]

        def expunge(self):
            raise AssertionError("a folder-wide EXPUNGE would erase the user's own deleted mail")

    imap = OldIMAP()
    assert mw._move_to_inbox(imap, b"7") is True
    assert [c[0] for c in imap.calls] == ["copy", "store", "expunge"] and imap.calls[-1] == ("expunge", b"7")


def test_move_is_used_when_the_server_names_it_only_after_login():
    class LoggedInIMAP:
        capabilities = ("IMAP4REV1", "IDLE")  # the greeting, before login

        def __init__(self):
            self.calls = []

        def capability(self):
            return "OK", [b"IMAP4rev1 IDLE UIDPLUS MOVE"]

        def uid(self, command, *args):
            self.calls.append((command, *args))
            return "OK", [b""]

    imap = LoggedInIMAP()
    assert mw._move_to_inbox(imap, b"9") is True
    assert imap.calls == [("move", b"9", "INBOX")]


def test_domain_check_finds_records():
    from app.services import mailbox_warmup as mw

    records = {
        "arvistry.com": ["v=spf1 include:_spf.mail.hostinger.com ~all"],
        "_dmarc.arvistry.com": ["v=DMARC1; p=none"],
        "hostingermail-a._domainkey.arvistry.com": ["v=DKIM1; k=rsa; p=ABC"],
    }
    out = mw.domain_check("ali@arvistry.com", resolver=lambda n: records.get(n, []))
    assert out == {"domain": "arvistry.com", "spf": True, "dkim": True, "dkim_selector": "hostingermail-a", "dmarc": True, "dmarc_policy": "none"}
    none = mw.domain_check("ali@x.com", resolver=lambda n: [])
    assert not (none["spf"] or none["dkim"] or none["dmarc"])


def test_instantly_style_ramp_and_reply_rate():
    from app.services import mailbox_warmup as mw

    assert [mw.daily_target(d, 10, 1) for d in (1, 2, 5, 10, 11, 30)] == [1, 2, 5, 10, 10, 10]
    assert [mw.daily_target(d, 30, 3) for d in (1, 2, 3, 10, 11)] == [1, 4, 7, 28, 30]
    assert mw.daily_target(4, 30) == 6  # no setting: the standard ramp
    assert mw.reply_rate({}) == 0.5 and mw.reply_rate({"warmup_reply_rate": None}) == 0.5
    assert mw.reply_rate({"warmup_reply_rate": 30}) == 0.3 and mw.reply_rate({"warmup_reply_rate": 0}) == 0
