"""Lead activity on the Leads page: emails sent, last send, and the best thing that happened across them."""
from app.services.lead_scoring import lead_activity


def log(recipient, event, sent_at, status="sent"):
    return {"recipient": recipient, "status": status, "last_event": event, "sent_at": sent_at}


def test_counts_sent_emails_and_keeps_the_latest_send():
    a = lead_activity([log("A@x.com", "delivered", "2026-10-01T09:00"), log("a@x.com", "opened", "2026-10-05T09:00"),
                       log("a@x.com", None, "2026-10-03T09:00"), log("a@x.com", None, "2026-10-09T09:00", status="failed")])
    assert a["a@x.com"] == {"emails_sent": 3, "last_sent_at": "2026-10-05T09:00", "engagement": "opened"}


def test_best_engagement_wins():
    a = lead_activity([log("b@x.com", "clicked", "1"), log("b@x.com", "replied", "2"), log("b@x.com", "delivered", "3")])
    assert a["b@x.com"]["engagement"] == "replied"


def test_a_bounce_shows_unless_they_engaged():
    a = lead_activity([log("c@x.com", "bounced", "1"), log("d@x.com", "bounced", "1"), log("d@x.com", "replied", "2"),
                       log("e@x.com", "delivered", "1"), log("e@x.com", "complained", "2")])
    assert a["c@x.com"]["engagement"] == "bounced"
    assert a["d@x.com"]["engagement"] == "replied"
    assert a["e@x.com"]["engagement"] == "complained"
