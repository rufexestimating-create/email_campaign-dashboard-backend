"""SPAM_DETECTOR_ENABLED switches the spam detector's endpoints off while the campaign API keeps working."""
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.config import settings
from app.main import app

SPAMMY = {"subject": "FREE QUOTE!!! act now", "body": "Get your FREE QUOTE TODAY!!", "content_type": "text"}


def client():
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    return TestClient(app, raise_server_exceptions=False)


def test_spam_endpoints_answer_404_when_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "SPAM_DETECTOR_ENABLED", False)
    c = client()
    try:
        for method, path, body in [("post", "/api/v1/spam/check", SPAMMY), ("post", "/api/v1/template/score", SPAMMY),
                                   ("post", "/api/v1/spam/check-batch", {"items": [SPAMMY]}), ("get", "/api/v1/spam/knowledge", None),
                                   ("get", "/api/v1/insights/model", None), ("get", "/api/v1/insights/model/calibration.png", None)]:
            res = getattr(c, method)(path, json=body) if body else getattr(c, method)(path)
            assert res.status_code == 404, path
        assert c.get("/api/v1/health").status_code == 200  # the rest of the API is untouched
    finally:
        app.dependency_overrides.clear()


def test_spam_score_still_works_when_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "SPAM_DETECTOR_ENABLED", True)
    c = client()
    try:
        res = c.post("/api/v1/spam/check", json=SPAMMY)
        assert res.status_code == 200 and res.json()["score"] >= 45
    finally:
        app.dependency_overrides.clear()
