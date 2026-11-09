"""Reading replies from the reply mailbox: what counts as a reply, and what text is kept."""
from datetime import datetime, timezone

from app.services.reply_inbox import parse, reply_text

NOW = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)


def message(body: str, extra_headers: str = "", sender: str = "Hamza Akram <HamzaAkram53454@gmail.com>") -> bytes:
    return (
        f"From: {sender}\r\nTo: ameer.hamza@hmdesigngroup.com\r\nSubject: Re: Following up\r\n"
        f"Date: Mon, 02 Nov 2026 21:51:00 +0500\r\nMessage-ID: <abc123@mail.gmail.com>\r\n{extra_headers}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    ).encode()


def test_a_gmail_reply_keeps_only_what_they_wrote():
    body = "interested, can we talk Tuesday?\r\n\r\nOn Mon, 2 Nov 2026 at 21:00, Arvistry <hello@arvistry.com> wrote:\r\n> Hi there,\r\n> Just checking"
    r = parse(message(body), NOW)
    assert r["from_email"] == "hamzaakram53454@gmail.com" and r["from_name"] == "Hamza Akram"
    assert r["snippet"] == "interested, can we talk Tuesday?"
    assert r["subject"] == "Re: Following up" and r["message_id"] == "<abc123@mail.gmail.com>"
    assert r["received_at"] == datetime(2026, 11, 2, 16, 51, tzinfo=timezone.utc)


def test_quoted_lines_and_outlook_headers_are_dropped():
    assert reply_text("Yes please\n> old text\n> more") == "Yes please"
    assert reply_text("Sounds good\n\nFrom: Arvistry\nSent: Monday\nOld email") == "Sounds good"


def test_html_only_reply():
    raw = (b"From: a@b.com\r\nSubject: Re: Hi\r\nMessage-ID: <h1@b.com>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<div>Not now, <b>thanks</b></div>")
    assert parse(raw, NOW)["snippet"] == "Not now, thanks"


def test_automatic_messages_are_not_replies():
    assert parse(message("I'm out of office", "Auto-Submitted: auto-replied\r\n"), NOW) is None
    assert parse(message("Away", "Precedence: auto_reply\r\n"), NOW) is None
    assert parse(message("Delivery failed", sender="Mail Delivery <mailer-daemon@googlemail.com>"), NOW) is None


def test_a_human_reply_marked_auto_submitted_no_counts():
    assert parse(message("Yes", "Auto-Submitted: no\r\n"), NOW)["snippet"] == "Yes"


def test_gmail_wraps_the_wrote_line_and_marks_bold():
    body = ('*what is the price?"*\r\n\r\nOn Mon, Nov 2, 2026 at 10:28 PM Rufex Estimating <rufexestimating@gmail.com>\r\n'
            "wrote:\r\n\r\n> interested share details.\r\n")
    assert reply_text(body) == 'what is the price?"'


def test_a_reply_that_starts_with_on_is_kept():
    assert reply_text("On Tuesday works for me.\n\nThanks") == "On Tuesday works for me. Thanks"


def test_full_text_keeps_the_whole_message_with_line_breaks():
    from app.services import reply_inbox as ri

    body = ("Hi Ameer,\r\n\r\nMany of your clients need expert permit plans.\r\nWe can provide work samples.\r\n\r\n\r\n\r\n"
            "[image: \u2713] Thanks,\r\nGary\r\n\r\nOn Mon, 9 Nov 2026 at 10:00, Ameer <a@x.com> wrote:\r\n> earlier email\r\n")
    text = ri.full_text(body)
    assert text == "Hi Ameer,\n\nMany of your clients need expert permit plans.\nWe can provide work samples.\n\n Thanks,\nGary"
    assert "wrote" not in text and "earlier" not in text
    assert len(ri.full_text("x" * 30000)) == ri.BODY_LENGTH
