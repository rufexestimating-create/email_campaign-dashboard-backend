import random
from datetime import datetime, timezone

import pytest

from app.services import pacing

START = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def gaps(times):
    return [(b - a).total_seconds() for a, b in zip(times, times[1:])]


@pytest.mark.parametrize("speed", ["safe", "normal"])
def test_paced_speeds_space_every_email_within_their_range(speed):
    lo, hi = pacing.SPEEDS[speed]
    times = pacing.send_times(200, speed, START, random.Random(1))
    assert len(times) == 200 and times[0] == START
    assert all(lo <= g <= hi for g in gaps(times))
    assert len({round(g, 3) for g in gaps(times)}) > 50  # random gaps, not a fixed machine rhythm


def test_safe_sends_about_150_an_hour():
    times = pacing.send_times(151, "safe", START, random.Random(2))
    hours = (times[-1] - START).total_seconds() / 3600
    assert 0.75 < hours < 1.3


def test_fast_sends_everything_at_once_and_unknown_speed_falls_back_to_safe():
    assert set(pacing.send_times(50, "fast", START)) == {START}
    lo, hi = pacing.SPEEDS["safe"]
    assert all(lo <= g <= hi for g in gaps(pacing.send_times(20, "turbo", START, random.Random(3))))


def test_no_recipients_means_no_send_times():
    assert pacing.send_times(0, "safe", START) == []


def test_any_spam_complaint_pauses_the_run():
    reason = pacing.pause_reason(sent=200, bounced=0, complained=1)
    assert reason and "marked this email as spam" in reason


def test_bounces_over_five_percent_pause_the_run():
    reason = pacing.pause_reason(sent=40, bounced=3, complained=0)
    assert reason and "3 of 40" in reason and "8%" in reason


@pytest.mark.parametrize("sent,bounced", [(10, 1), (100, 5), (0, 0), (200, 2)])
def test_a_single_typo_or_a_low_bounce_rate_does_not_pause(sent, bounced):
    assert pacing.pause_reason(sent, bounced, 0) is None
