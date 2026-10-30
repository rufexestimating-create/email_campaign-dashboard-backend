"""Tests for send-time / behaviour analysis (M6). Run from backend/: python -m pytest"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import campaign_advisor
from app.services import send_time as st

UTC = timezone.utc
# Wednesday 14 January 2026, 15:00 UTC = 9 AM in Chicago, 10 AM in New York (winter time)
WED_15_UTC = datetime(2026, 1, 14, 15, 0, tzinfo=UTC)

LEADS = {
    "gc@build.com": {"email": "gc@build.com", "persona": "contractor", "timezone": "America/Chicago"},
    "arch@studio.com": {"email": "arch@studio.com", "persona": "architect", "timezone": "America/New_York"},
    "home@gmail.com": {"email": "home@gmail.com", "persona": "homeowner", "timezone": "America/Los_Angeles"},
    "nozone@x.com": {"email": "nozone@x.com", "persona": None, "timezone": None},
}


def event(recipient, kind, when, device=None, email_id=None):
    return {"recipient": recipient, "event_type": kind, "occurred_at": when.isoformat(), "device": device, "email_id": email_id}


# ---------- Rows and local time ----------

def test_rows_use_clicks_and_replies_in_local_time():
    rows = st.engagement_rows([
        event("gc@build.com", "clicked", WED_15_UTC, "mobile"),
        event("arch@studio.com", "replied", WED_15_UTC, "proxy"),
        event("arch@studio.com", "opened", WED_15_UTC, "desktop"),  # opens are never counted
        event("GC@build.com", "delivered", WED_15_UTC),
    ], LEADS)
    assert len(rows) == 2
    gc, arch = rows
    assert (gc["weekday"], gc["hour"], gc["device"], gc["weight"], gc["persona"]) == (2, 9, "mobile", 1, "contractor")
    assert (arch["hour"], arch["device"], arch["weight"]) == (10, "unknown", 3)  # proxy hides the device


def test_unknown_timezone_falls_back_and_is_flagged():
    (row,) = st.engagement_rows([event("nozone@x.com", "clicked", WED_15_UTC)], LEADS, default_tz="America/Denver")
    assert row["hour"] == 8 and row["timezone"] == "America/Denver" and row["timezone_known"] is False
    assert row["persona"] == "other"


def test_heatmaps_have_totals_and_weights():
    rows = st.engagement_rows([event("gc@build.com", "clicked", WED_15_UTC, "mobile"), event("gc@build.com", "replied", WED_15_UTC)], LEADS)
    maps = st.heatmaps(rows)
    assert maps["all"]["all"]["grid"][2][9] == 4  # 1 click + 3 for a reply
    assert maps["contractor"]["mobile"]["clicks"] == 1 and maps["contractor"]["unknown"]["replies"] == 1
    assert "architect" not in maps  # combinations without data are left out


# ---------- Windows ----------

def test_best_window_picks_busy_days_and_two_hour_block():
    grid = st.empty_grid()
    grid[1][9] = grid[1][10] = 5   # Tue 9-11
    grid[3][9] = grid[3][10] = 3   # Thu 9-11: 60% of Tuesday, so it joins
    grid[0][9] = 5                 # Mon: only 50% of Tuesday, left out
    grid[5][20] = 1                # one stray Saturday evening click
    w = st.best_window(grid)
    assert w == {"days": (1, 3), "start": 9, "end": 11}
    assert st.window_label(w) == "Tue, Thu, 9 AM–11 AM"
    assert st.window_label(st.DEFAULT_WINDOWS["contractor"]) == "Mon–Thu, 6 AM–8 AM"
    assert st.best_window(st.empty_grid()) is None


def test_lead_profile_needs_three_events():
    two = [event("arch@studio.com", "clicked", WED_15_UTC + timedelta(days=7 * i)) for i in range(2)]
    assert st.lead_profiles(st.engagement_rows(two, LEADS)) == {}
    five = [event("arch@studio.com", "clicked", WED_15_UTC + timedelta(days=7 * i), "desktop") for i in range(5)]
    p = st.lead_profiles(st.engagement_rows(five, LEADS))["arch@studio.com"]
    assert p["confidence"] == "good" and p["main_device"] == "desktop" and p["window"]["days"] == (2,) and p["window"]["start"] in (9, 10)
    assert st.lead_profiles(st.engagement_rows(five[:3], LEADS))["arch@studio.com"]["confidence"] == "low"


def test_window_fallback_order():
    rows = st.engagement_rows([event("gc@build.com", "clicked", WED_15_UTC + timedelta(days=7 * i)) for i in range(3)], LEADS)
    profiles, segments = st.lead_profiles(rows), st.segment_windows(rows)
    assert st.window_for("gc@build.com", LEADS["gc@build.com"], profiles, segments)["source"] == "lead_profile"
    other = st.window_for("home@gmail.com", LEADS["home@gmail.com"], profiles, segments)
    assert other["source"] == "default" and other["window"]["start"] == 19

    many = [event(f"h{i}@gmail.com", "clicked", WED_15_UTC) for i in range(st.MIN_SEGMENT_EVENTS)]
    leads = {f"h{i}@gmail.com": {"persona": "homeowner", "timezone": "America/Chicago"} for i in range(st.MIN_SEGMENT_EVENTS)}
    segments = st.segment_windows(st.engagement_rows(many, leads))
    learned = st.window_for("home@gmail.com", LEADS["home@gmail.com"], {}, segments)
    assert learned["source"] == "persona_history" and learned["window"]["days"] == (2,)


def test_next_window_start_is_in_recipient_time():
    w = st.DEFAULT_WINDOWS["architect"]  # Tue-Thu 9-11
    at = st.next_window_start(WED_15_UTC, "America/New_York", w)  # it's Wed 10 AM there: today's 9 AM has passed
    assert (at.weekday(), at.hour) == (3, 9) and at.utcoffset() == timedelta(hours=-5)
    early = st.next_window_start(datetime(2026, 1, 13, 13, 50, tzinfo=UTC), "America/New_York", w)  # Tue 8:50 AM: within 15 min
    assert (early.weekday(), early.hour) == (2, 9)


# ---------- Reply delays ----------

@pytest.mark.parametrize("hours,wait", [(None, None), (1.5, None), (3, 24), (8, 24), (12, 48), (40, 48)])
def test_follow_up_rule(hours, wait):
    assert st.follow_up_after_hours(hours) == wait


def test_reply_delays_per_lead_and_overall():
    sent = {"e1": "2026-01-14T15:00:00+00:00", "e2": "2026-01-15T15:00:00+00:00", "e3": "2026-01-14T15:00:00+00:00"}
    replies = [
        event("arch@studio.com", "replied", WED_15_UTC + timedelta(hours=5), email_id="e1"),
        event("arch@studio.com", "replied", WED_15_UTC + timedelta(days=1, hours=7), email_id="e2"),
        event("gc@build.com", "replied", WED_15_UTC + timedelta(minutes=30), email_id="e3"),
        event("gc@build.com", "replied", WED_15_UTC, email_id="unknown"),  # no matching send: skipped
    ]
    d = st.reply_delays(replies, sent)
    assert d["replies_measured"] == 3 and d["median_hours"] == 5.0 and d["follow_up_after_hours"] == 24
    assert d["per_lead"]["arch@studio.com"]["median_hours"] == 6.0 and d["per_lead"]["arch@studio.com"]["follow_up_after_hours"] == 24
    assert d["per_lead"]["gc@build.com"]["follow_up_after_hours"] is None


def test_plan_recipient_uses_lead_delay():
    delays = st.reply_delays([event("arch@studio.com", "replied", WED_15_UTC + timedelta(hours=20), email_id="e1")], {"e1": WED_15_UTC.isoformat()})
    # Wed 10 AM in New York is inside the architect window (Tue-Thu 9-11): the best time is now
    p = st.plan_recipient("arch@studio.com", LEADS["arch@studio.com"], {}, {}, delays, WED_15_UTC)
    assert p["source"] == "default" and p["window"] == "Tue–Thu, 9 AM–11 AM"
    assert p["open_now"] is True and p["send_at_utc"].startswith("2026-01-14T15:00:00")
    assert p["follow_up_after_hours"] == 48 and "48 hours" in p["follow_up_note"]

    # Wed noon is past the window: the next one is Thursday 9 AM New York
    later = st.plan_recipient("arch@studio.com", LEADS["arch@studio.com"], {}, {}, delays, WED_15_UTC + timedelta(hours=2))
    assert later["open_now"] is False
    assert later["send_at_local"].startswith("2026-01-15T09:00:00-05:00") and later["send_at_utc"].startswith("2026-01-15T14:00:00")


# ---------- Pre-send timing ----------

def test_timing_check_counts_recipients_inside_their_window():
    # Wed 15:00 UTC: 9 AM for the Chicago contractor (window 6-8: miss), 10 AM for the NY architect (9-11: hit)
    t = st.timing_check(["gc@build.com", "arch@studio.com"], LEADS, {}, {}, WED_15_UTC, WED_15_UTC)
    assert (t["in_window"], t["total"], t["good"], t["learned"]) == (1, 2, True, False)

    t = st.timing_check(["home@gmail.com"], LEADS, {}, {}, WED_15_UTC, WED_15_UTC)  # 7 AM in LA, homeowners read at 7-9 PM
    assert not t["good"] and "7 PM" in t["best_label"] and "Los Angeles" in t["best_label"]
    assert (t["suggestion"].hour, t["suggestion"].weekday()) == (19, 2)


def test_precheck_uses_recipient_windows(monkeypatch):
    monkeypatch.setattr(campaign_advisor, "_history", lambda user_id: [])
    monkeypatch.setattr(st, "load_leads", lambda user_id: LEADS)
    monkeypatch.setattr(st, "load_engagement", lambda user_id, days: [])
    monkeypatch.setattr(st, "load_sent_at", lambda user_id, ids: {})
    send_at = datetime(2030, 1, 16, 4, 0, tzinfo=UTC)  # Tuesday 8 PM in Los Angeles
    result = campaign_advisor.precheck("u1", {
        "subject": "Ideas for your kitchen remodel", "body": "Hi {{first_name}}, a few ideas for your kitchen.", "content_type": "text",
        "recipients": ["home@gmail.com"], "reply_to": "hello@arvistry.com",
        "schedule": {"type": "once", "send_at": send_at, "timezone": "America/Los_Angeles"},
    })
    timing = next(r for r in result["recommendations"] if r["id"].startswith("timing"))
    assert timing["id"] == "timing_good" and "1 of 1 recipient" in timing["detail"]
    assert result["learned_from_history"] is False
