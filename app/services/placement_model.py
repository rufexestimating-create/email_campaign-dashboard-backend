"""Combines the rules-based spam score with the trained placement model (ml/train.py).

Final spam score = model weight × model probability + (1 − model weight) × rule score. The model weighs less while it is
trained on synthetic or proxy labels. Without a saved model (or if it fails) the rule score is used on its own.
Reasons come from the model's coefficients: each feature's coefficient × its value for this email, averaged over the
calibration folds, so they explain this email rather than the model in general.
"""
import json
import logging
import re
import threading
from pathlib import Path

import numpy as np

from app.services import spam_checker

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "ml" / "models"
MODEL_FILE_RE = re.compile(r"^spam_lr_v(\d+)(_synthetic)?\.joblib$")
# A model trained on demo data only echoes hand-written patterns, so it may nudge the rule score but not override it
MODEL_WEIGHT = {"real": 0.6, "proxy": 0.5, "synthetic": 0.25}
REASON_LIMIT = 5
MIN_IMPACT = 0.1  # smaller pushes are noise (single common words), not something worth showing the writer
# Label sources that observe the inbox directly; anything else (complaints, bounces, manual notes) is a proxy
DIRECT_SOURCES = {"seed_test"}

# (label when the value is high / present, label when low / absent). None = not a meaningful reason in that direction.
NUMERIC_LABELS = {
    "subject_length": ("Long subject line", None),
    "word_count": ("Long message", "Very short message"),
    "links": ("Many links", None),
    "images": ("Many images", None),
    "exclamations": ("Exclamation marks", None),
    "money_signs": ("Money amounts ($)", None),
    "caps_ratio": ("Lots of capital letters", None),
    "has_unsubscribe": (None, "No unsubscribe line"),
    "has_address": (None, "No postal address"),
    "personalised": (None, "Not personalised"),
    "kw_high": ("High-risk spam phrases", None),
    "kw_medium": ("Medium-risk spam phrases", None),
    "kw_low": ("Low-risk spam phrases", None),
    "kw_in_subject": ("Spam phrases in the subject line", None),
    "issue_subject_caps": ("Subject in capital letters", None),
    "issue_subject_exclaim": ("Exclamation marks in the subject", None),
    "issue_fake_reply": ("Fake RE:/FW: subject", None),
    "issue_subject_emoji": ("Several emojis in the subject", None),
    "issue_subject_money": ("Money amount in the subject", None),
    "issue_body_caps": ("Words in ALL CAPS", None),
    "issue_body_multi_exclaim": ("Repeated exclamation marks", None),
    "issue_spaced_out": ("Spaced-out letters", None),
    "issue_shortener": ("Link shortener", None),
    "issue_ip_link": ("Link to an IP address", None),
    "issue_many_links": ("Too many links", None),
    "issue_image_heavy": ("Image-heavy email", None),
    "issue_html_unsafe": ("Forms or scripts in the HTML", None),
    "issue_no_plain_text": ("No plain-text version", None),
}

_lock = threading.Lock()
_cache: dict = {"key": None, "model": None}


def latest_model_path(models_dir: Path | None = None) -> Path | None:
    found = [(int(m.group(1)), p) for p in (models_dir or MODELS_DIR).glob("spam_lr_v*.joblib") if (m := MODEL_FILE_RE.match(p.name))]
    return max(found)[1] if found else None


def _label_kind(meta: dict) -> str:
    if meta.get("data_source") != "real":
        return "synthetic"
    sources = set((meta.get("label_sources") or {}).keys())
    return "real" if sources and sources <= DIRECT_SOURCES else "proxy"


def load_model(models_dir: Path | None = None) -> dict | None:
    """Latest saved model with its metadata, reloaded only when a newer file appears."""
    path = latest_model_path(models_dir)
    if path is None:
        return None
    key = (str(path), path.stat().st_mtime)
    with _lock:
        if _cache["key"] != key:
            import joblib  # heavy import, only when a model exists

            model = joblib.load(path)
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            folds = []
            for cc in model.calibrated_classifiers_:
                pipe = cc.estimator
                folds.append((pipe.named_steps["columns"], pipe.named_steps["columns"].get_feature_names_out(), pipe.named_steps["clf"].coef_[0]))
            _cache.update(key=key, model={"estimator": model, "meta": meta, "folds": folds, "kind": _label_kind(meta)})
        return _cache["model"]


def _reason_label(name: str, value: float) -> str | None:
    kind, _, feature = name.partition("__")
    if kind == "numeric":
        high, low = NUMERIC_LABELS.get(feature, (None, None))
        return high if value > 0 else low
    where = "subject line" if kind == "subject" else "message"
    return f"{'Phrase' if ' ' in feature else 'Word'} \"{feature}\" in the {where}"


def explain(loaded: dict, features) -> list[dict]:
    """Top reasons pushing this email toward spam: coefficient × feature value, averaged over the folds."""
    totals: dict[str, list[float]] = {}
    for columns, names, coef in loaded["folds"]:
        x = columns.transform(features)
        row = np.asarray(x[0].toarray()).ravel() if hasattr(x, "toarray") else np.asarray(x[0]).ravel()
        for i in np.flatnonzero(row):
            t = totals.setdefault(names[i], [0.0, 0.0])
            t[0] += row[i] * coef[i] / len(loaded["folds"])
            t[1] = row[i]
    reasons = []
    for name, (impact, value) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
        if impact < MIN_IMPACT or len(reasons) == REASON_LIMIT:
            break
        label = _reason_label(name, value)
        if label:
            reasons.append({"feature": name, "label": label, "impact": round(float(impact), 3)})
    return reasons


def predict(subject: str, body: str, content_type: str = "text") -> dict | None:
    try:
        loaded = load_model()
        if loaded is None:
            return None
        import pandas as pd

        from ml.features import build_features

        email = pd.DataFrame([{"subject": subject, "body": body, "content_type": content_type}])
        probability = float(loaded["estimator"].predict_proba(email)[0, 1])
        features = build_features(email)
        meta = loaded["meta"]
        return {
            "probability": round(probability, 4),
            "spam_percent": round(probability * 100),
            "version": meta["version"],
            "data_source": meta["data_source"],
            "label_kind": loaded["kind"],
            "weight": MODEL_WEIGHT[loaded["kind"]],
            "reasons": explain(loaded, features),
        }
    except Exception:
        logger.exception("Placement model failed; using the rule score only")
        return None


def warm_up():
    """Load scikit-learn and the model before the first editor request (the cold load takes several seconds)."""
    predict("Warm-up", "Loading the placement model.")


def score(subject: str, body: str, content_type: str = "text", user_id: str | None = None) -> dict:
    """spam_checker.analyze, with score / level / label replaced by the rules + model blend when a model is available."""
    result = spam_checker.analyze(subject, body, content_type, user_id)
    result["rule_score"] = result["score"]
    model = predict(subject, body, content_type)
    result["model"] = model
    if model is None:
        return result

    blended = min(100, round(model["weight"] * model["probability"] * 100 + (1 - model["weight"]) * result["rule_score"]))
    level = "good" if blended < 20 else "fair" if blended < 45 else "poor"
    estimated = model["label_kind"] != "real"
    result.update(
        score=blended,
        inbox_score=100 - blended,
        level=level,
        risk={"good": "low", "fair": "medium", "poor": "high"}[level],
        label={"good": "Looks good", "fair": "Needs a few changes", "poor": "Likely to land in spam"}[level],
        placement_label="Estimated placement" if estimated else "Predicted placement",
        estimate_note=(
            "Estimate from the writing checks and the "
            + ("starter prediction model" if model["label_kind"] == "synthetic"
               else "prediction model, learned partly from complaints and bounces" if model["label_kind"] == "proxy"
               else "prediction model, learned from your inbox tests")
            + ". No tool can guarantee inbox placement."
        ),
    )
    return result
