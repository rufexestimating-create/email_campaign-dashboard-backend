"""Tests for the domain warm-up schedule and how it feeds the pre-send volume check. Run from backend/: python -m pytest"""
from datetime import date, datetime, timezone

import pytest

from app.services import preflight as pf
from app.services import warmup

START = date(2026, 10, 1)


@pytest.mark.parametrize("day,week,limit", [(0, 1, 20), (6, 1, 20), (7, 2, 40), (20, 3, 75), (27, 4, 100), (34, 5, 100), (35, 6, 100), (200, 29, 100)])
def test_limit_grows_week_by_week_capped_by_account(day, week, limit):
    p = warmup.plan(START, date.fromordinal(START.toordinal() + day), True, account_limit=100)
    assert p["week"] == week and p["limit"] == limit
    assert p["warming"] == (week <= len(warmup.SCHEDULE))


def test_bigger_account_limit_lets_later_weeks_grow():
    p = warmup.plan(START, date(2026, 10, 29), True, account_limit=1000)  # day 28 = week 5
    assert (p["week"], p["limit"]) == (5, 300)
    assert [r["per_day"] for r in p["schedule"]] == [20, 40, 75, 150, 300, 1000]
    assert [r["current"] for r in p["schedule"]] == [False, False, False, False, True, False]
    assert p["schedule"][1]["from"] == "2026-10-08"


def test_disabled_or_future_start_uses_account_limit():
    assert warmup.plan(START, date(2026, 10, 2), False, 100)["limit"] == 100
    future = warmup.plan(date(2026, 12, 1), date(2026, 10, 2), True, 100)
    assert future["limit"] == 100 and future["week"] is None


def test_volume_check_uses_warm_up_limit():
    now = datetime(2026, 10, 29, 15, tzinfo=timezone.utc)
    history = [{"recipient": f"p{i}@x.com", "status": "sent", "last_event": "delivered", "sent_at": now.replace(hour=9).isoformat()} for i in range(15)]
    c = pf.volume_spike(10, history, now, sending_now=True, daily_limit=20, warm_week=1)
    assert c["status"] == "fail" and "week 1 of its warm-up" in c["detail"] and c["data"]["daily_limit"] == 20
    assert pf.volume_spike(5, history, now, sending_now=True, daily_limit=20, warm_week=1)["status"] != "fail"
