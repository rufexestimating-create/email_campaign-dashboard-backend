"""Tests for the pre-send decision log (M8) and lead scoring (M9). Run from backend/: python -m pytest"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import lead_scoring as ls
from app.services import preflight as pf
from ml import lead_train

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


# ---------- M8: decisions ----------

def checks(*statuses):
    return [{"id": f"c{i}", "status": s, "message": f"check {i}"} for i, s in enumerate(statuses)]


def test_failed_send_needs_a_reason():
    with pytest.raises(pf.OverrideReasonRequired):
        pf.decision_row("u1", {"verdict": "fail", "checks": checks("fail", "pass"), "accepted": [], "outcome": "sent", "override_reason": "ok"})
    row = pf.decision_row("u1", {"verdict": "fail", "checks": checks("fail", "warn", "pass"), "accepted": ["c1"], "outcome": "sent",
                                 "override_reason": "  Client asked for it by phone  "})
    assert row["overridden"] is True and row["override_reason"] == "Client asked for it by phone"
    assert row["accepted"] == ["c1"] and row["ignored"] == ["c0"]


def test_cancelled_or_clean_sends_are_not_overrides():
    row = pf.decision_row("u1", {"verdict": "fail", "checks": checks("fail"), "accepted": [], "outcome": "cancelled", "override_reason": None})
    assert row["overridden"] is False and row["override_reason"] is None and row["ignored"] == ["c0"]
    row = pf.decision_row("u1", {"verdict": "warn", "checks": checks("warn", "pass"), "accepted": ["c0", "c0"], "outcome": "scheduled", "override_reason": "x"})
    assert row["overridden"] is False and row["override_reason"] is None and row["accepted"] == ["c0"] and row["ignored"] == []


def test_decision_summary_counts_accepted_and_ignored():
    rows = [
        {"accepted": ["fatigue"], "ignored": ["send_time"], "overridden": False, "outcome": "sent"},
        {"accepted": [], "ignored": ["send_time", "content_fit"], "overridden": True, "outcome": "sent"},
        {"accepted": [], "ignored": ["send_time"], "overridden": False, "outcome": "cancelled"},
    ]
    s = pf.decision_summary(rows)
    assert s["decisions"] == 3 and s["overridden"] == 1 and s["cancelled"] == 1
    assert s["per_check"]["send_time"] == {"accepted": 0, "ignored": 3}
    assert s["per_check"]["fatigue"] == {"accepted": 1, "ignored": 0}


# ---------- M9: features ----------

def at(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_features_from_sends_and_events():
    lead = {"email": "Sam@Builders.com", "persona": "contractor", "company": "Acme", "timezone": "America/Chicago", "lifecycle_stage": "sql"}
    sends = [{"recipient": "sam@builders.com", "status": "sent", "sent_at": at(d)} for d in (30, 10, 2)]
    events = [
        {"recipient": "sam@builders.com", "event_type": "clicked", "occurred_at": at(9), "device": "mobile"},
        {"recipient": "sam@builders.com", "event_type": "clicked", "occurred_at": at(1), "device": "desktop"},
        {"recipient": "sam@builders.com", "event_type": "replied", "occurred_at": at(1.5), "device": None},
    ]
    (row,) = ls.build_rows([lead], sends, events, NOW)
    f = row["features"]
    assert (f["sends"], f["clicks"], f["replies"], f["click_rate"]) == (3, 2, 1, round(2 / 3, 4))
    assert f["days_since_first_send"] == 30 and f["days_since_last_click"] == 1 and f["mobile_share"] == 0.5
    assert (f["company_domain"], f["has_company"], f["persona_contractor"], f["persona_other"]) == (1, 1, 1, 0)
    assert row["labels"] == {"reply": 1, "mql": 1, "sql": 1}


def test_never_emailed_lead():
    (row,) = ls.build_rows([{"email": "info@gmail.com"}], [], [], NOW)
    f = row["features"]
    assert f["sends"] == 0 and f["days_since_last_click"] == ls.NEVER_DAYS and f["role_based"] == 1 and f["company_domain"] == 0
    assert f["persona_other"] == 1 and row["labels"] == {"reply": 0, "mql": 0, "sql": 0}


def test_reply_model_never_sees_replies():
    assert "replies" not in ls.FEATURES["reply"] and "days_since_last_reply" not in ls.FEATURES["reply"]
    assert "replies" in ls.FEATURES["sql"]


# ---------- M9: segments and bands ----------

def feat(sends=3, clicks=0, replies=0, last_click=ls.NEVER_DAYS, last_reply=ls.NEVER_DAYS):
    return {"sends": sends, "clicks": clicks, "replies": replies, "days_since_last_click": last_click, "days_since_last_reply": last_reply}


def test_rule_segments():
    assert ls.rule_segment(feat(sends=0)) == "New"
    assert ls.rule_segment(feat()) == "Cold"
    assert ls.rule_segment(feat(clicks=1, replies=1, last_click=20, last_reply=5)) == "Champions"
    assert ls.rule_segment(feat(clicks=2, last_click=20)) == "Engaged"
    assert ls.rule_segment(feat(clicks=1, last_click=80)) == "Cooling"


def test_kmeans_segments_once_enough_leads_engage():
    rows = [feat(clicks=5, replies=2, last_click=2, last_reply=3) for _ in range(12)] + \
           [feat(clicks=1, last_click=100) for _ in range(12)] + [feat() for _ in range(10)] + [feat(sends=0)]
    segments, method = ls.segment_all(rows)
    assert method == "kmeans"
    assert segments[0] == "Champions" and segments[-1] == "New"
    assert segments[0] != segments[12] and segments[12] != segments[24]
    assert ls.segment_all(rows[:5])[1] == "rules"


@pytest.mark.parametrize("p,band", [(0.61, "sales"), (0.6, "nurture"), (0.3, "nurture"), (0.29, "awareness"), (None, None)])
def test_bands(p, band):
    assert ls.band_for(p) == band


# ---------- M9: training and prediction ----------

def test_train_compares_models_and_predicts(tmp_path, monkeypatch):
    df = lead_train.synthetic(600, seed=4)
    assert set(df["y_sql"]) == {0, 1} and (df["y_sql"] <= df["y_mql"]).all()  # every SQL is also an MQL
    meta = lead_train.train(df, source="synthetic", note="test", out_dir=tmp_path)
    assert meta["data_source"] == "synthetic" and meta["warning"] and (tmp_path / "lead_scoring_v1_synthetic.joblib").exists()
    for t in ls.TARGETS:
        c = meta["comparison"][t]
        assert c["logistic_regression"]["pr_auc"] is not None and c["winner"] in ("logistic_regression", "lightgbm", "hist_gradient_boosting")
        assert c["explanations"] == {"lightgbm": "shap", "logistic_regression": "coefficients", "hist_gradient_boosting": "none"}[c["winner"]]

    bundle = ls.load_bundle(tmp_path)
    hot = {**{k: 0 for k in ls.BASE_FEATURES + ls.REPLY_FEATURES}, "sends": 6, "clicks": 4, "click_rate": 0.67, "days_since_last_click": 2,
           "replies": 2, "days_since_last_reply": 3, "company_domain": 1, "has_company": 1, "persona_contractor": 1, "days_since_first_send": 40}
    cold = {**hot, "clicks": 0, "click_rate": 0, "days_since_last_click": ls.NEVER_DAYS, "replies": 0, "days_since_last_reply": ls.NEVER_DAYS,
            "company_domain": 0, "has_company": 0, "persona_contractor": 0, "persona_other": 1}
    p_hot, p_cold = ls.predict(bundle, [hot, cold])
    assert p_hot["p_sql"] > p_cold["p_sql"] and p_hot["p_mql"] > p_cold["p_mql"]
    assert all(0 <= p_hot[f"p_{t}"] <= 1 for t in ls.TARGETS)
    assert p_hot["drivers"]["sql"] and p_hot["drivers"]["sql"][0]["impact"] > 0


def test_lead_probabilities_are_calibrated(tmp_path):
    """Bands use fixed thresholds (P(SQL) > 0.6), so the average prediction must match the real rate on unseen leads."""
    lead_train.train(lead_train.synthetic(1500, seed=11), source="synthetic", out_dir=tmp_path)
    bundle = ls.load_bundle(tmp_path)
    unseen = lead_train.synthetic(2000, seed=99)
    preds = ls.predict(bundle, unseen[ls.BASE_FEATURES + ls.REPLY_FEATURES].to_dict("records"))
    for t in ls.TARGETS:
        mean_p = sum(p[f"p_{t}"] for p in preds) / len(preds)
        assert abs(mean_p - unseen[f"y_{t}"].mean()) < 0.05, t
    assert bundle["meta"]["comparison"]["sql"]["calibration"]["method"] == "isotonic"


def test_gbm_is_lightgbm_or_sklearn_fallback():
    name, model = lead_train.gbm_model()
    assert name in ("lightgbm", "hist_gradient_boosting") and hasattr(model, "fit")
