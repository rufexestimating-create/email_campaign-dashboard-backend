"""Tests for how a campaign email is built for one recipient. Run from backend/: python -m pytest"""
import pytest

from app.services.campaign_service import campaign_service, personal_values, tidy_subject, unsubscribe_header

CAMPAIGN = {
    "from_email": "Arvistry <hello@arvistry.com>", "reply_to": None, "content_type": "text", "ab_enabled": False,
    "subject_a": "Welcome aboard, {{first_name}}!", "body_a": "Hi {{first_name}},\n\nThanks for joining.",
}


def test_missing_name_reads_naturally():
    msg = campaign_service._build_message(CAMPAIGN, {"email": "sam@x.com", "name": None, "variables": {}, "variant": "A"})
    assert msg["subject"] == "Welcome aboard!"
    assert msg["text"].startswith("Hi there,")


def test_name_is_used_when_known():
    msg = campaign_service._build_message(CAMPAIGN, {"email": "sam@x.com", "name": "Sam Lee", "variables": {}, "variant": "A"})
    assert msg["subject"] == "Welcome aboard, Sam!" and msg["text"].startswith("Hi Sam,")


@pytest.mark.parametrize("raw,clean", [
    ("Welcome aboard, !", "Welcome aboard!"),
    (", it's finally here", "It's finally here"),
    ("Quick follow-up, ", "Quick follow-up"),
    ("Ideas for your kitchen remodel", "Ideas for your kitchen remodel"),
])
def test_tidy_subject(raw, clean):
    assert tidy_subject(raw) == clean


def test_unsubscribe_header_uses_reply_to_then_sender():
    assert unsubscribe_header(CAMPAIGN) == "<mailto:hello@arvistry.com?subject=unsubscribe>"
    assert unsubscribe_header({**CAMPAIGN, "reply_to": "team@arvistry.com"}) == "<mailto:team@arvistry.com?subject=unsubscribe>"
    msg = campaign_service._build_message(CAMPAIGN, {"email": "sam@x.com", "name": "Sam", "variables": {}, "variant": "A"})
    assert msg["headers"]["List-Unsubscribe"].startswith("<mailto:")


def test_greeting_fallback_only_when_asked():
    assert personal_values({"email": "a@b.com"})["first_name"] == ""
    assert personal_values({"email": "a@b.com"}, greeting_fallback=True)["first_name"] == "there"
