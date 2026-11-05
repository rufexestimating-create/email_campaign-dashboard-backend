"""Tests for automated follow-ups (M10) and the server-side pre-send block. Run from backend/: python -m pytest"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.main import app
from app.services import follow_ups as fu
from app.services import preflight, send_time

WED_15_UTC = datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)  # 10 AM in New York


def ctx(delays=None, profiles=None, segments=None):
    return {"delays": delays or {"per_lead": {}, "follow_up_after_hours": None}, "profiles": profiles or {}, "segments": segments or {},
            "default_tz": "America/New_York", "leads": {}}


# ---------- Stop rules ----------

@pytest.mark.parametrize("state,reason", [
    ({}, None),
    ({"stage": "new", "band": "nurture"}, None),
    ({"stage": "mql"}, None),
    ({"replied": True}, "Replied"),
    ({"bounced": True}, "Email bounced"),
    ({"complained": True}, "Marked an email as spam"),
    ({"stage": "sql"}, "Moved to sales-qualified"),
    ({"stage": "customer"}, "Became a customer"),
    ({"stage": "lost"}, "Marked as lost"),
    ({"band": "sales"}, "Sales-ready: handed to sales"),
    ({"replied": True, "stage": "lost"}, "Replied"),  # the reply is the more useful reason to show
])
def test_stop_reason(state, reason):
    assert fu.stop_reason(state) == reason


# ---------- Timing ----------

def test_wait_stretches_for_slow_repliers():
    assert fu.wait_hours(3, None) == 72
    assert fu.wait_hours(1, 48) == 48   # replies usually take 12+ hours: wait two days, not one
    assert fu.wait_hours(3, 24) == 72   # the chosen wait already covers it


def test_due_time_lands_in_the_contacts_best_window():
    architect = {"persona": "architect", "timezone": "America/New_York"}  # default window Tue-Thu 9-11 AM
    due = fu.due_time(WED_15_UTC, 3, "arch@studio.com", architect, ctx())
    # Wed 10 AM + 3 days = Sat 10 AM -> next window start is Tuesday 9 AM New York (14:00 UTC)
    assert due == datetime(2026, 1, 20, 14, 0, tzinfo=timezone.utc)

    homeowner = {"persona": "homeowner", "timezone": "America/Los_Angeles"}  # Tue-Thu 7-9 PM
    due = fu.due_time(WED_15_UTC, 1, "home@gmail.com", homeowner, ctx())
    assert due == datetime(2026, 1, 16, 3, 0, tzinfo=timezone.utc)  # Thursday 7 PM in Los Angeles


def test_due_time_uses_the_contacts_reply_delay():
    lead = {"persona": "other", "timezone": "America/New_York"}  # Tue-Thu 8-11 AM
    slow = ctx(delays={"per_lead": {"x@y.com": {"follow_up_after_hours": 48}}, "follow_up_after_hours": None})
    fast = fu.due_time(WED_15_UTC, 1, "x@y.com", lead, ctx())
    late = fu.due_time(WED_15_UTC, 1, "x@y.com", lead, slow)
    assert fast == datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)   # Thursday 10 AM: already inside the window, send then
    assert late == datetime(2026, 1, 20, 13, 0, tzinfo=timezone.utc)   # Friday is out of window -> Tuesday 8 AM


def test_unknown_timezone_uses_the_default():
    # No timezone: the default (New York) is used. Thu 10 AM there is inside the 8-11 AM window
    due = fu.due_time(WED_15_UTC, 1, "x@y.com", {"persona": None, "timezone": None}, ctx())
    assert send_time.to_local(due, "America/New_York").hour == 10


# ---------- Server-side pre-send block (closes the gap in the pre-send review) ----------

@pytest.fixture
def client(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    created = []
    from app.services.campaign_service import campaign_service
    monkeypatch.setattr(campaign_service, "create", lambda user_id, data: created.append(data) or {"id": "c1", "status": "scheduled"})
    yield TestClient(app), created
    app.dependency_overrides.clear()


CAMPAIGN = {
    "name": "Test", "subject": "Hello", "body": "Hi there", "recipients": [{"email": "a@b.com"}],
    "schedule": {"type": "once", "send_at": "2030-01-15T15:00:00Z", "timezone": "America/New_York"},
}


def test_failed_review_blocks_send_without_reason(client, monkeypatch):
    c, created = client
    monkeypatch.setattr(preflight, "preflight", lambda user_id, data: {"verdict": "fail", "checks": [{"name": "Audience health", "status": "fail"}]})
    r = c.post("/api/v1/campaigns", json=CAMPAIGN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "preflight_failed" and not created
    assert c.post("/api/v1/campaigns", json={**CAMPAIGN, "override_reason": "Client asked by phone"}).status_code == 201
    assert c.post("/api/v1/campaigns", json={**CAMPAIGN, "save_as_draft": True}).status_code == 201  # drafts aren't reviewed


def test_passing_review_or_broken_checks_dont_block(client, monkeypatch):
    c, created = client
    monkeypatch.setattr(preflight, "preflight", lambda user_id, data: {"verdict": "warn", "checks": []})
    assert c.post("/api/v1/campaigns", json=CAMPAIGN).status_code == 201

    def broken(user_id, data):
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(preflight, "preflight", broken)
    assert c.post("/api/v1/campaigns", json=CAMPAIGN).status_code == 201
    assert len(created) == 2


def test_follow_ups_validate():
    r = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    try:
        too_many = {**CAMPAIGN, "save_as_draft": True, "follow_ups": [{"wait_days": 3, "subject": "s", "body": "b"}] * 3}
        assert r.post("/api/v1/campaigns", json=too_many).status_code == 422
        for wait in (-1, 31):  # 0 is allowed: a test follow-up, about 10 minutes later
            bad_wait = {**CAMPAIGN, "save_as_draft": True, "follow_ups": [{"wait_days": wait, "subject": "s", "body": "b"}]}
            assert r.post("/api/v1/campaigns", json=bad_wait).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_a_test_follow_up_goes_ten_minutes_later_whatever_the_window():
    slow = ctx(delays={"per_lead": {"x@y.com": {"follow_up_after_hours": 48}}, "follow_up_after_hours": 72})
    due = fu.due_time(WED_15_UTC, 0, "x@y.com", {"persona": "architect", "timezone": "America/New_York"}, slow)
    assert due == WED_15_UTC + timedelta(minutes=fu.TEST_WAIT_MINUTES)


def test_the_composer_accepts_a_test_follow_up():
    from app.schemas.campaign import FollowUpIn

    assert FollowUpIn(wait_days=0, subject="s", body="b").wait_days == 0


def test_a_wait_in_minutes_is_sent_right_after_it():
    due = fu.due_time(WED_15_UTC, 0, "x@y.com", None, ctx(), wait_minutes=1)
    assert due == WED_15_UTC + timedelta(minutes=1)
    assert fu.due_time(WED_15_UTC, 0, "x@y.com", None, ctx(), wait_minutes=60) == WED_15_UTC + timedelta(hours=1)


def test_days_only_follow_ups_do_not_send_the_minutes_column(monkeypatch):
    inserted = []

    class T:
        def table(self, _):
            return self

        def insert(self, rows):
            inserted.extend(rows)
            return self

        def execute(self):
            return self

    monkeypatch.setattr(fu, "_db", lambda: T())
    fu.save_steps("u", "c", [{"wait_days": 3, "subject": "s", "body": "b"}, {"wait_days": 0, "wait_minutes": 1, "subject": "s", "body": "b"}])
    assert "wait_minutes" not in inserted[0] and inserted[1]["wait_minutes"] == 1
