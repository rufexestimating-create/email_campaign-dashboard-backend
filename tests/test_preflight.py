"""Tests for the campaign preflight (M7). Run from backend/: python -m pytest"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.services import campaign_advisor, preflight as pf, send_time

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


def sent(recipient, hours_ago, last_event="delivered", status="sent"):
    return {"recipient": recipient, "status": status, "last_event": last_event, "sent_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


# ---------- Individual checks ----------

def test_audience_health():
    history = [sent("bad@x.com", 100, "bounced"), sent("ok@x.com", 100)]
    c = pf.audience_health(["bad@x.com", "ok@x.com", "info@firm.com"], history)
    assert c["status"] == "fail" and c["action"]["emails"] == ["bad@x.com"]
    c = pf.audience_health(["ok@x.com", "info@firm.com"], history)
    assert c["status"] == "warn" and c["action"]["emails"] == ["info@firm.com"]
    c = pf.audience_health(["ok@x.com"], history)
    assert c["status"] == "pass" and c["data"]["engagement_tracked"] is False


def test_never_engaged_only_counts_once_tracking_works():
    engaged = [sent(f"fan{i}@x.com", 50, "clicked") for i in range(pf.MIN_ENGAGED_FOR_LEARNING)]
    cold = [sent("cold@x.com", h) for h in (30, 60, 90)]
    c = pf.audience_health(["cold@x.com"], engaged + cold)
    assert c["status"] == "warn" and c["action"]["emails"] == ["cold@x.com"]
    assert pf.audience_health(["cold@x.com"], cold)["status"] == "pass"


def test_frequency_fatigue():
    history = [sent("a@x.com", 10), sent("b@x.com", 100), sent("b@x.com", 120), sent("b@x.com", 140), sent("c@x.com", 200)]
    c = pf.frequency_fatigue(["a@x.com", "b@x.com", "c@x.com"], history, NOW)
    assert c["status"] == "fail" and c["action"]["emails"] == ["b@x.com"]
    c = pf.frequency_fatigue(["a@x.com", "c@x.com"], history, NOW)
    assert c["status"] == "warn" and c["action"]["emails"] == ["a@x.com"]
    assert pf.frequency_fatigue(["c@x.com"], history, NOW)["status"] == "pass"


def test_gmail_domain_risk():
    clean = [sent(f"p{i}@x.com", 24) for i in range(400)]
    assert pf.gmail_domain_risk(["a@company.com"], clean, True, NOW)["status"] == "pass"  # no Gmail recipients
    assert pf.gmail_domain_risk(["a@gmail.com"], clean, True, NOW)["status"] == "pass"
    assert pf.gmail_domain_risk(["a@gmail.com"], clean, False, NOW)["status"] == "warn"  # no unsubscribe line
    complaints = clean + [sent(f"angry{i}@gmail.com", 24, "complained") for i in range(2)]
    c = pf.gmail_domain_risk(["a@gmail.com"], complaints, True, NOW)
    assert c["status"] == "fail" and c["data"]["complaint_rate"] == pytest.approx(2 / 402, abs=1e-4)


def test_volume_spike(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_SEND_LIMIT", 100)
    history = [sent(f"p{d}-{i}@x.com", 24 * d + 1) for d in range(1, 8) for i in range(20)] + [sent(f"t{i}@x.com", 1) for i in range(90)]
    assert pf.volume_spike(20, history, NOW, sending_now=True)["status"] == "fail"   # only 10 left today
    assert pf.volume_spike(20, history, NOW, sending_now=False)["status"] == "pass"  # typical day is 20
    assert pf.volume_spike(80, history, NOW, sending_now=False)["status"] == "warn"  # 4x the usual day


def test_predicted_placement_mixes_labels_per_provider():
    labels = [{"provider": "outlook", "placement": "spam"}] * 6 + [{"provider": "outlook", "placement": "inbox"}] * 4
    c = pf.predicted_placement(["a@gmail.com", "b@gmail.com", "c@outlook.com"], 0.1, labels, "estimate")
    gmail, outlook = c["data"]["providers"]
    assert (gmail["name"], gmail["spam_probability"], gmail["basis"]) == ("Gmail", 0.1, "model")
    assert outlook["spam_probability"] == pytest.approx(0.35) and outlook["basis"] == "model + your placement labels"
    assert c["status"] == "warn" and c["data"]["estimated"] is True
    assert pf.predicted_placement(["a@gmail.com"], 0.6, [], "")["status"] == "fail"


def fake_dns(records):
    return lambda name: records.get(name, [])


GOOD_DNS = {
    "send.arvistry.com": ["v=spf1 include:amazonses.com ~all"],
    "resend._domainkey.arvistry.com": ["p=MIGfMA0GCSqGSIb3DQEB"],
    "_dmarc.arvistry.com": ["v=DMARC1; p=none; rua=mailto:hello@arvistry.com"],
}


@pytest.fixture(autouse=True)
def fresh_dns_cache():
    pf._dns_cache.clear()


def test_authentication():
    assert pf.sender_domain("Arvistry <hello@arvistry.com>") == "arvistry.com"
    c = pf.authentication("arvistry.com", fake_dns(GOOD_DNS))
    assert c["status"] == "pass" and c["data"]["dmarc_policy"] == "none"
    pf._dns_cache.clear()
    c = pf.authentication("arvistry.com", fake_dns({k: v for k, v in GOOD_DNS.items() if not k.startswith("_dmarc")}))
    assert c["status"] == "warn" and "DMARC" in c["message"]
    pf._dns_cache.clear()
    assert pf.authentication("arvistry.com", fake_dns({}))["status"] == "fail"
    assert pf.authentication("resend.dev")["status"] == "warn"

    def broken(name):
        raise OSError("no network")
    pf._dns_cache.clear()
    assert pf.authentication("arvistry.com", broken)["status"] == "warn"


def test_content_fit_offers_unsubscribe_fix():
    from app.services import spam_checker

    plain = spam_checker.analyze("Kitchen remodel ideas", "Hi Sam, a few ideas for your kitchen layout and lighting. Would Tuesday suit a call?")
    c = pf.content_fit({"A": plain})
    assert c["status"] == "warn" and c["action"]["type"] == "apply_fix" and c["action"]["version"] == "A"
    spammy = spam_checker.analyze("FREE QUOTE!!! act now", "FREE QUOTE TODAY!! https://bit.ly/x $99 $199 $299")
    assert pf.content_fit({"A": plain, "B": spammy})["status"] == "fail"


# ---------- Full run ----------

def test_preflight_runs_all_eight_checks(monkeypatch):
    monkeypatch.setattr(campaign_advisor, "_history", lambda user_id: [sent("bounced@gmail.com", 50, "bounced")])
    monkeypatch.setattr(send_time, "load_leads", lambda user_id: {})
    monkeypatch.setattr(send_time, "load_engagement", lambda user_id, days: [])
    monkeypatch.setattr(send_time, "load_sent_at", lambda user_id, ids: {})
    monkeypatch.setattr(pf, "load_placement_labels", lambda user_id: [])
    monkeypatch.setattr(pf.warmup, "daily_limit", lambda user_id: (100, None))
    monkeypatch.setattr(pf, "dns_txt", fake_dns(GOOD_DNS))
    result = pf.preflight("u1", {
        "subject": "Ideas for your kitchen remodel",
        "body": "Hi {{first_name}}, a few ideas for your kitchen.\n\nDon't want these emails? Reply \"unsubscribe\".\n1200 Main Street, Austin, TX 78701",
        "content_type": "text", "recipients": ["bounced@gmail.com", "sam@builders.com"], "reply_to": None, "from_email": "hello@arvistry.com",
        "schedule": {"type": "once", "send_at": datetime(2030, 1, 15, 15, 0, tzinfo=timezone.utc), "timezone": "America/New_York"},
    }, now=NOW)
    ids = [c["id"] for c in result["checks"]]
    assert ids == ["audience_health", "send_time", "frequency_fatigue", "gmail_domain_risk", "volume_spike", "predicted_placement", "authentication", "content_fit"]
    assert all(c["status"] in ("pass", "warn", "fail") and c["message"] for c in result["checks"])
    assert result["verdict"] == "fail" and result["counts"]["fail"] >= 1  # the bounced contact
    assert result["checks"][0]["action"] == {"type": "remove_recipients", "emails": ["bounced@gmail.com"], "label": "Remove 1 contact"}
    assert result["checks"][6]["status"] == "pass"
    assert result["recipient_count"] == 2 and "A" in result["spam"]


def test_preflight_without_the_spam_detector_runs_the_six_campaign_checks(monkeypatch):
    """SPAM_DETECTOR_ENABLED=false: no spam scoring at all, the two content checks drop out, the rest are unchanged."""
    monkeypatch.setattr(settings, "SPAM_DETECTOR_ENABLED", False)
    monkeypatch.setattr(pf.placement_model, "score", lambda *a, **k: pytest.fail("spam model must not run"))
    monkeypatch.setattr(campaign_advisor, "_history", lambda user_id: [sent("bounced@gmail.com", 50, "bounced")])
    monkeypatch.setattr(send_time, "load_leads", lambda user_id: {})
    monkeypatch.setattr(send_time, "load_engagement", lambda user_id, days: [])
    monkeypatch.setattr(send_time, "load_sent_at", lambda user_id, ids: {})
    monkeypatch.setattr(pf, "load_placement_labels", lambda user_id: [])
    monkeypatch.setattr(pf.warmup, "daily_limit", lambda user_id: (100, None))
    monkeypatch.setattr(pf, "dns_txt", fake_dns(GOOD_DNS))
    data = {
        "subject": "Ideas for your kitchen remodel", "body": "Hi {{first_name}}, a few ideas for your kitchen.",
        "content_type": "text", "recipients": ["bounced@gmail.com", "sam@builders.com"], "reply_to": None, "from_email": "hello@arvistry.com",
        "schedule": {"type": "once", "send_at": datetime(2030, 1, 15, 15, 0, tzinfo=timezone.utc), "timezone": "America/New_York"},
    }
    result = pf.preflight("u1", data, now=NOW)
    assert [c["id"] for c in result["checks"]] == ["audience_health", "send_time", "frequency_fatigue", "gmail_domain_risk", "volume_spike", "authentication"]
    assert result["spam"] == {} and result["verdict"] == "fail"  # the bounced contact still blocks the send
    gmail = next(c for c in result["checks"] if c["id"] == "gmail_domain_risk")
    assert gmail["status"] != "pass"  # no unsubscribe line is still caught without the spam detector
    advice = campaign_advisor.precheck("u1", data)
    assert advice["spam"] == {} and not any(r["id"].startswith("spam_") for r in advice["recommendations"])