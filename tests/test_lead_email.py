"""A hand-written email to one lead."""
from app.core.config import settings
from app.services.lead_email import build_message


def test_answering_a_reply_threads_it_and_fills_the_name(monkeypatch):
    monkeypatch.setattr(settings, "REPLY_TO_EMAIL", "inbox@example.com")
    m = build_message({"email": "sam@x.com", "name": "Sam Lee"}, "Re: Ideas", "Hi {{first_name}}, here are the prices.", "<abc@mail.gmail.com>")
    assert m["to"] == ["sam@x.com"] and m["subject"] == "Re: Ideas"
    assert m["text"] == "Hi Sam, here are the prices." and "Hi Sam" in m["html"]
    assert m["headers"]["In-Reply-To"] == m["headers"]["References"] == "<abc@mail.gmail.com>"
    assert m["reply_to"] == "inbox@example.com" and "List-Unsubscribe" in m["headers"]


def test_a_new_follow_up_starts_its_own_conversation(monkeypatch):
    monkeypatch.setattr(settings, "REPLY_TO_EMAIL", "")
    m = build_message({"email": "a@x.com", "name": None}, "Following up", "Hi {{first_name}}")
    assert m["text"] == "Hi there" and "In-Reply-To" not in m["headers"] and "reply_to" not in m
