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


# ---------- A person's own email, written for them in the composer ----------

def test_a_person_can_get_their_own_subject_and_message():
    own = {"_subject": "A note for you, {{first_name}}", "_body": "Hi {{first_name}},\n\nThis one is just for you."}
    msg = campaign_service._build_message(CAMPAIGN, {"email": "sam@x.com", "name": "Sam Lee", "variables": own, "variant": "A"})
    assert msg["subject"] == "A note for you, Sam" and msg["text"].startswith("Hi Sam,\n\nThis one is just for you.")
    other = campaign_service._build_message(CAMPAIGN, {"email": "ann@x.com", "name": "Ann", "variables": {}, "variant": "A"})
    assert other["subject"] == "Welcome aboard, Ann!"  # everyone else gets the campaign's email


def test_an_own_html_message_is_not_escaped():
    html = {**CAMPAIGN, "content_type": "html"}
    msg = campaign_service._build_message(html, {"email": "sam@x.com", "name": "Sam", "variables": {"_body": "<p>Hi <b>{{first_name}}</b></p>"}, "variant": "A"})
    assert "<b>Sam</b>" in msg["html"]


def test_follow_ups_ignore_the_own_message():
    r = {"email": "sam@x.com", "name": "Sam", "variables": {"_subject": "Own", "_body": "Own body"}, "variant": "A"}
    step = {**CAMPAIGN, "subject_a": "Following up", "body_a": "Just checking in."}
    msg = campaign_service._build_message(step, r, own_content=False)
    assert msg["subject"] == "Following up" and msg["text"] == "Just checking in."


def test_own_email_is_kept_whole_and_a_csv_cannot_fake_it():
    from app.schemas.campaign import CampaignCreate

    long_body = "word " * 400  # 2000 characters: CSV values are cut at 500, an own message isn't
    data = CampaignCreate(
        name="t", subject="Main", body="Main body", schedule={"type": "now"},
        recipients=[{"email": "sam@x.com", "name": "Sam", "subject": "Mine", "body": long_body},
                    {"email": "ann@x.com", "variables": {"_subject": "from a csv column"}}],
    )
    sam, ann = campaign_service._clean_recipients(data)
    assert sam["variables"]["_subject"] == "Mine" and sam["variables"]["_body"] == long_body
    assert "_subject" not in ann["variables"] and ann["variables"]["subject"] == "from a csv column"
