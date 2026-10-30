"""Tests for the spam-vs-inbox model (M4). Run from backend/: python -m pytest"""
import json

import joblib
import pandas as pd

from ml.data import label_for, synthetic_dataset
from ml.features import NUMERIC_FEATURES, build_features
from ml.train import INPUT_COLUMNS, next_version, time_split, train

SPAMMY = {"subject": "FREE QUOTE!!! act now", "body": "Get your FREE QUOTE TODAY!! Click here https://bit.ly/3xRemodel, save $500, prices from $99!", "content_type": "text"}
CLEAN = {
    "subject": "Concept drawings for your bathroom remodel",
    "body": "Hi {{first_name}},\n\nWe put together a short case study of a similar remodel. Would a 15-minute call on Tuesday work?\n\n"
            "Best regards,\nHamza, Arvistry\n\nDon't want these emails? Reply \"unsubscribe\" and we'll take you off our list.\n"
            "Arvistry Design Studio, 1200 Example Street, Suite 300, Austin, TX 78701",
    "content_type": "text",
}


def test_label_mapping():
    assert label_for("spam") == 1 and label_for("complained") == 1
    assert label_for("inbox") == 0 and label_for("promotions") == 0
    assert label_for("missing") is None and label_for("bounced") is None


def test_features_have_every_column():
    out = build_features(pd.DataFrame([SPAMMY, CLEAN]))
    assert list(out.columns) == ["subject", "body_text"] + NUMERIC_FEATURES
    spam, clean = out.iloc[0], out.iloc[1]
    assert spam["issue_shortener"] == 1 and clean["issue_shortener"] == 0
    assert spam["kw_high"] > clean["kw_high"]
    assert clean["has_unsubscribe"] == 1 and clean["has_address"] == 1
    assert "{{" not in clean["body_text"]


def test_synthetic_dataset_is_marked_and_reproducible():
    a, b = synthetic_dataset(100, seed=7), synthetic_dataset(100, seed=7)
    assert a.equals(b)
    assert set(a["source"]) == {"SYNTHETIC"}
    assert set(a["label"]) == {0, 1}


def test_time_split_keeps_newest_for_testing():
    df = synthetic_dataset(50, seed=1).sample(frac=1, random_state=0)
    train_df, test_df = time_split(df)
    assert len(test_df) == 10
    assert train_df["sent_at"].max() <= test_df["sent_at"].min()


def test_train_saves_versioned_model_that_loads(tmp_path):
    df = synthetic_dataset(240, seed=3)
    meta = train(df, source="synthetic", note="test", out_dir=tmp_path)
    assert meta["version"] == 1 and meta["data_source"] == "synthetic" and meta["warning"]
    assert (tmp_path / "spam_lr_v1_synthetic.joblib").exists()
    assert (tmp_path / "calibration_v1_synthetic.png").exists()
    saved = json.loads((tmp_path / "spam_lr_v1_synthetic.json").read_text(encoding="utf-8"))
    assert saved["metrics"]["roc_auc"] is not None and saved["top_coefficients"]["toward_spam"]

    model = joblib.load(tmp_path / "spam_lr_v1_synthetic.joblib")
    p_spam, p_clean = model.predict_proba(pd.DataFrame([SPAMMY, CLEAN])[INPUT_COLUMNS])[:, 1]
    assert 0 <= p_clean < p_spam <= 1

    assert next_version(tmp_path) == 2
    assert train(df, source="real", out_dir=tmp_path)["files"]["model"] == "spam_lr_v2.joblib"
