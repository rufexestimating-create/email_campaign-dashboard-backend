"""Paced ("drip") sending: when each recipient of a campaign run goes out, and when a run should stop itself.

Sending a whole list in one burst is what filters see from compromised accounts, and it spends a bad list before
anyone notices. Spacing emails out looks like a person sending, and the first few dozen sends show whether the list
bounces or annoys people, so the run can pause before the rest of it goes out.
"""
import random
from datetime import datetime, timedelta

# Seconds between two emails (min, max). Random within the range, because an exact rhythm looks automated.
SPEEDS: dict[str, tuple[float, float]] = {"safe": (15, 30), "normal": (5, 10), "fast": (0, 0)}
DEFAULT_SPEED = "safe"

# A run pauses itself on any spam complaint, or once bounces pass 5% (counted from the second bounce, so a single
# typo in a short list doesn't stop it).
MAX_BOUNCE_RATE = 0.05
MIN_BOUNCES = 2


def send_times(count: int, speed: str, start: datetime, rng: random.Random | None = None) -> list[datetime]:
    """One send time per recipient: the first at `start`, each next one a random gap later."""
    lo, hi = SPEEDS.get(speed, SPEEDS[DEFAULT_SPEED])
    rng = rng or random.Random()
    times, t = [], start
    for i in range(count):
        if i:
            t += timedelta(seconds=rng.uniform(lo, hi))
        times.append(t)
    return times


def pause_reason(sent: int, bounced: int, complained: int) -> str | None:
    """Why this run should stop sending, judged on the emails sent so far in it; None to carry on."""
    if complained:
        who = "1 person" if complained == 1 else f"{complained} people"
        return (f"Paused automatically: {who} marked this email as spam. Check the message and the list "
                "before sending the rest, because more complaints can get your domain blocked.")
    if bounced >= MIN_BOUNCES and sent and bounced / sent > MAX_BOUNCE_RATE:
        return (f"Paused automatically: {bounced} of {sent} emails bounced ({round(bounced / sent * 100)}%). "
                "The list has bad addresses; sending the rest could hurt your domain's reputation.")
    return None
