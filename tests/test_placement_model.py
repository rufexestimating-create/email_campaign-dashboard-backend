"""Tests for combining the rule score with the placement model (M5). Run from backend/: python -m pytest"""
import pytest

from app.services import placement_model as pm
from ml.data import synthetic_dataset
from ml.train import train

SPAMMY = ("FREE QUOTE!!! act now", "Get your FREE QUOTE TODAY!! Click here https://bit.ly/3xRemodel, save $500, prices from $99!")
CLEAN = (
    "Concept drawings for your bathroom remodel",
    "Hi {{first_name}},\n\nWe put together a short case study of a similar remodel. Would a 15-minute call on Tuesday work?\n\n"
    "Best regards,\nHamza, Arvistry\n\nDon't want these emails? Reply \"unsubscribe\" and we'll take you off our list.\n"
    "Arvistry Design Studio, 1200 Example Street, Suite 300, Austin, TX 78701",
)


@pytest.fixture(scope="module")
def models_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("models")
    train(synthetic_dataset(300, seed=5), source="synthetic", note="test", out_dir=out)
    return out


@pytest.fixture
def no_models(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MODELS_DIR", tmp_path)


def test_without_a_model_the_rule_score_is_used(no_models):
    r = pm.score(*SPAMMY)
    assert r["model"] is None
    assert r["score"] == r["rule_score"]
    assert "placement_label" not in r


def test_blends_model_and_rules(models_dir, monkeypatch):
    monkeypatch.setattr(pm, "MODELS_DIR", models_dir)
    spam, clean = pm.score(*SPAMMY), pm.score(*CLEAN)
    for r in (spam, clean):
        m = r["model"]
        assert m["version"] == 1 and m["label_kind"] == "synthetic" and m["weight"] == pm.MODEL_WEIGHT["synthetic"]
        expected = round(m["weight"] * m["probability"] * 100 + (1 - m["weight"]) * r["rule_score"])
        assert r["score"] == min(100, expected) and r["inbox_score"] == 100 - r["score"]
        assert r["placement_label"] == "Estimated placement" and "starter prediction model" in r["estimate_note"]
    assert spam["model"]["probability"] > clean["model"]["probability"]
    assert spam["level"] == "poor" and clean["level"] == "good"


def test_reasons_explain_this_email(models_dir, monkeypatch):
    monkeypatch.setattr(pm, "MODELS_DIR", models_dir)
    reasons = pm.score(*SPAMMY)["model"]["reasons"]
    assert 0 < len(reasons) <= pm.REASON_LIMIT
    assert all(r["impact"] >= pm.MIN_IMPACT for r in reasons)
    assert [r["impact"] for r in reasons] == sorted((r["impact"] for r in reasons), reverse=True)
    labels = " ".join(r["label"] for r in reasons)
    assert "unsubscribe" in labels.lower() or "spam phrases" in labels.lower() or "Link shortener" in labels


def test_reason_labels_follow_direction():
    assert pm._reason_label("numeric__has_unsubscribe", -1.2) == "No unsubscribe line"
    assert pm._reason_label("numeric__has_unsubscribe", 0.8) is None  # having one never counts against the email
    assert pm._reason_label("numeric__kw_high", 2.0) == "High-risk spam phrases"
    assert pm._reason_label("subject__free", 0.4) == 'Word "free" in the subject line'
    assert pm._reason_label("body__act now", 0.4) == 'Phrase "act now" in the message'


def test_label_kind():
    assert pm._label_kind({"data_source": "synthetic"}) == "synthetic"
    assert pm._label_kind({"data_source": "real", "label_sources": {"seed_test": 300}}) == "real"
    assert pm._label_kind({"data_source": "real", "label_sources": {"seed_test": 200, "complaint": 40}}) == "proxy"


def test_latest_model_wins(tmp_path):
    for name in ("spam_lr_v1_synthetic.joblib", "spam_lr_v3.joblib", "spam_lr_v2_synthetic.joblib", "notes.joblib"):
        (tmp_path / name).write_bytes(b"")
    assert pm.latest_model_path(tmp_path).name == "spam_lr_v3.joblib"
