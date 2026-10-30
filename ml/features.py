"""Features shared by training and live scoring.

The numeric signals come straight from app/services/spam_checker.analyze, so the model sees an email exactly the
way the rules-based scorer does. build_features is the first step of the saved pipeline: at scoring time (M5) the
model is given only subject, body and content_type.
"""
from functools import lru_cache

import pandas as pd

from app.services.spam_checker import PLACEHOLDER_RE, analyze, to_plain_text

TEXT_COLUMNS = ["subject", "body_text"]
# Rule checks that matter as yes/no signals (money, caps and link counts are already numeric features below)
ISSUE_FLAGS = [
    "subject_caps", "subject_exclaim", "fake_reply", "subject_emoji", "subject_money", "body_caps", "body_multi_exclaim",
    "spaced_out", "shortener", "ip_link", "many_links", "image_heavy", "html_unsafe", "no_plain_text",
]
NUMERIC_FEATURES = [
    "subject_length", "word_count", "links", "images", "exclamations", "money_signs", "caps_ratio",
    "has_unsubscribe", "has_address", "personalised", "kw_high", "kw_medium", "kw_low", "kw_in_subject",
] + [f"issue_{i}" for i in ISSUE_FLAGS]


# Scoring runs the same email through every calibration fold, so the rule pass is cached rather than repeated
@lru_cache(maxsize=512)
def numeric_features(subject: str, body: str, content_type: str = "text") -> dict:
    r = analyze(subject, body, content_type)
    s = r["stats"]
    by_severity = {"high": 0, "medium": 0, "low": 0}
    for k in r["keywords"]:
        by_severity[k["severity"]] += k["count"]
    issue_ids = {i["id"] for i in r["issues"]}
    return {
        "subject_length": s["subject_length"],
        "word_count": s["word_count"],
        "links": s["links"],
        "images": s["images"],
        "exclamations": s["exclamations"],
        "money_signs": s["money_signs"],
        "caps_ratio": s["caps_ratio"],
        "has_unsubscribe": int(s["has_unsubscribe"]),
        "has_address": int(s["has_address"]),
        "personalised": int(s["personalised"]),
        "kw_high": by_severity["high"],
        "kw_medium": by_severity["medium"],
        "kw_low": by_severity["low"],
        "kw_in_subject": sum(k["count"] for k in r["keywords"] if k["location"] == "subject"),
        **{f"issue_{i}": int(i in issue_ids) for i in ISSUE_FLAGS},
    }


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """subject, body, content_type -> the text columns for TF-IDF plus the numeric rule signals."""
    rows = []
    for r in df.to_dict("records"):
        subject, body = r.get("subject") or "", r.get("body") or ""
        content_type = r.get("content_type") or "text"
        rows.append({
            "subject": PLACEHOLDER_RE.sub(" ", subject),
            "body_text": PLACEHOLDER_RE.sub(" ", to_plain_text(body, content_type)),
            **numeric_features(subject, body, content_type),
        })
    return pd.DataFrame(rows, columns=TEXT_COLUMNS + NUMERIC_FEATURES)
