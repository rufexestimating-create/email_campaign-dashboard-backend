"""Tests for webhook verification, event parsing, lead profiles and local time. Run from backend/: python -m pytest"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import event_tracking as et
from app.services.local_time import clean_state, clean_zip, state_for_zip, timezone_for, to_local

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-0123456789").decode()


def sign(body: bytes, msg_id="msg_1", stamp=None, secret=SECRET) -> dict:
    stamp = str(stamp if stamp is not None else int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": stamp, "svix-signature": f"v1,{sig}"}


# ---------- Signature ----------

def test_valid_signature_passes():
    body = b'{"type":"email.opened"}'
    et.verify_signature(SECRET, sign(body), body)


def test_signature_accepts_any_of_several():
    body = b"{}"
    headers = sign(body)
    headers["svix-signature"] = "v1,bm90LWl0 " + headers["svix-signature"]  # key rotation sends two
    et.verify_signature(SECRET, headers, body)


@pytest.mark.parametrize("tamper", ["body", "secret", "old", "missing"])
def test_bad_signatures_are_rejected(tamper):
    body = b'{"type":"email.clicked"}'
    headers = sign(body, stamp=int(time.time()) - 3600 if tamper == "old" else None)
    if tamper == "missing":
        headers.pop("svix-signature")
    other_secret = "whsec_" + base64.b64encode(b"another-key").decode()
    with pytest.raises(et.WebhookSignatureError):
        et.verify_signature(other_secret if tamper == "secret" else SECRET, headers, body + (b" " if tamper == "body" else b""))


# ---------- Parsing helpers ----------

@pytest.mark.parametrize("ua,device", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "mobile"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari", "mobile"),
    ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", "tablet"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", "desktop"),
    ("Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)", "proxy"),
    (None, None),
])
def test_device_from_user_agent(ua, device):
    assert et.device_from_user_agent(ua) == device


def test_provider_and_persona():
    assert et.provider_for("Sam@GMAIL.com") == "gmail"
    assert et.provider_for("a@hotmail.com") == "outlook"
    assert et.provider_for("a@acme-builders.com") == "other"
    assert et.normalize_persona("General Contractor") == "contractor"
    assert et.normalize_persona("Interior designer") == "architect"
    assert et.normalize_persona("Home owner") == "homeowner"
    assert et.normalize_persona("Business owner") == "other"
    assert et.normalize_persona("") is None


def test_parse_time_is_utc():
    assert et.parse_time("2026-10-28T14:00:00.000Z") == datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
    assert et.parse_time("2026-10-28T09:00:00-05:00") == datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
    assert et.parse_time("not a date") is None


def test_lead_fields_from_spreadsheet_columns():
    f = et.lead_fields({"zip_code": "78701-1234", "role": "GC", "company": "Acme Builders"})
    assert f == {"company": "Acme Builders", "persona": "contractor", "zip": "78701", "state": "TX", "timezone": "America/Chicago"}
    assert et.lead_fields({"state": "California"})["timezone"] == "America/Los_Angeles"
    assert et.lead_fields({"timezone": "Not/AZone", "state": "NY"})["timezone"] == "America/New_York"
    assert et.lead_fields({}) == {}


# ---------- Local time ----------

def test_zip_state_timezone():
    assert clean_zip("8901") == "08901"
    assert state_for_zip("08901") == "NJ"
    assert state_for_zip("10001") == "NY"
    assert state_for_zip("90210") == "CA"
    assert clean_state("tx") == "TX" and clean_state("New York") == "NY" and clean_state("XX") is None
    assert timezone_for("79901") == "America/Denver"        # El Paso is on Mountain time
    assert timezone_for("32501") == "America/Chicago"       # Pensacola, Florida panhandle
    assert timezone_for("37902") == "America/New_York"      # Knoxville, East Tennessee
    assert timezone_for(state="AZ") == "America/Phoenix"
    assert timezone_for() is None


def test_to_local_handles_dst():
    winter = to_local(datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc), "America/New_York")
    summer = to_local(datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc), "America/New_York")
    assert (winter.hour, summer.hour) == (10, 11)
    assert to_local(datetime(2026, 7, 15, 15, 0), "Bad/Zone").hour == 15  # naive = UTC, unknown zone = UTC


# ---------- Webhook endpoint ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", SECRET)
    seen = []
    monkeypatch.setattr(et, "handle_resend_event", lambda payload, svix_id: seen.append((payload, svix_id)) or "stored")
    return TestClient(app), seen


def test_webhook_stores_signed_event(client):
    c, seen = client
    body = json.dumps({"type": "email.clicked", "data": {"email_id": "abc"}}).encode()
    r = c.post("/api/v1/webhooks/resend", content=body, headers={**sign(body, msg_id="msg_9"), "content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {"result": "stored"}
    assert seen == [({"type": "email.clicked", "data": {"email_id": "abc"}}, "msg_9")]


def test_webhook_rejects_unsigned(client):
    c, seen = client
    r = c.post("/api/v1/webhooks/resend", json={"type": "email.clicked"})
    assert r.status_code == 401 and seen == []


def test_webhook_refuses_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", "")
    assert TestClient(app).post("/api/v1/webhooks/resend", json={}).status_code == 503


def test_unknown_event_types_are_ignored():
    assert et.handle_resend_event({"type": "contact.created", "data": {}}, "x") == "ignored"
