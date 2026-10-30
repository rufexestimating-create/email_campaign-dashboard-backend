"""Train the lead-scoring models (M9): P(reply), P(MQL), P(SQL).

For each target: logistic regression vs gradient boosting (LightGBM, or scikit-learn's HistGradientBoosting when
LightGBM can't load on this machine), compared on PR-AUC over a time-based split (older leads train, newer test).
The better model per target is saved. SHAP explanations are only used when LightGBM wins.

Run from backend/:
    python -m ml.lead_train               # real lead outcomes if there are enough, otherwise SYNTHETIC demo data
    python -m ml.lead_train --synthetic   # force the synthetic demo set
"""
import argparse
import json
import math
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.services import lead_scoring as ls

MODELS_DIR = Path(__file__).resolve().parent / "models"
VERSION_RE = re.compile(r"^lead_scoring_v(\d+)(?:_synthetic)?\.joblib$")
TEST_FRACTION = 0.2
MIN_LEADS = 100
MIN_POSITIVES = 10
CALIBRATION_FOLDS = 3
CALIBRATION_ISOTONIC_MIN = 1000  # isotonic needs plenty of data; smaller sets use the steadier sigmoid (Platt) fit


def gbm_model():
    """(library name, unfitted classifier). On Windows LightGBM needs an OpenMP runtime: the Visual C++ redistributable,
    or the copy scikit-learn bundles, which is loaded by importing sklearn (done at the top of this module) first."""
    try:
        from lightgbm import LGBMClassifier

        return "lightgbm", LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=10,
                                          class_weight="balanced", random_state=0, verbose=-1)
    except (ImportError, OSError):
        from sklearn.ensemble import HistGradientBoostingClassifier

        return "hist_gradient_boosting", HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                                        min_samples_leaf=10, class_weight="balanced", random_state=0)


def logistic_model() -> Pipeline:
    return Pipeline([("scale", StandardScaler()), ("clf", LogisticRegression(class_weight="balanced", max_iter=2000))])


# ---------- Data ----------

def load_real() -> pd.DataFrame:
    """Every user's leads with features, outcomes and created_at (for the time split)."""
    from app.db.supabase import get_supabase

    users = [u["id"] for u in get_supabase().table("users").select("id").execute().data]
    records = []
    for user_id in users:
        leads, logs, events = ls.load_user_data(user_id)
        for r in ls.build_rows(leads, logs, events):
            records.append({**r["features"], **{f"y_{t}": v for t, v in r["labels"].items()}, "created_at": pd.Timestamp(r["lead"]["created_at"])})
    return pd.DataFrame(records)


def synthetic(n: int = 1500, seed: int = 42) -> pd.DataFrame:
    """SYNTHETIC demo leads: a hidden interest level drives clicks, replies and qualification. Not real behaviour."""
    rng = random.Random(seed)
    base = datetime(2025, 11, 1, tzinfo=timezone.utc)
    sig = lambda z: 1 / (1 + math.exp(-z))
    rows = []
    for i in range(n):
        persona = rng.choices(ls.PERSONAS, weights=(35, 25, 30, 10))[0]
        company = rng.random() < (0.85 if persona in ("contractor", "architect") else 0.25)
        interest = rng.gauss(0, 1) + (0.4 if persona == "contractor" else 0.2 if persona == "architect" else 0)
        sends = rng.randint(1, 12)
        bounced = rng.random() < 0.04
        click_p = 0 if bounced else sig(interest - 1.6) * 0.6
        clicks = sum(rng.random() < click_p for _ in range(sends))
        replies = 0 if bounced else sum(rng.random() < sig(interest - 2.4) * 0.5 for _ in range(sends))
        last_click = rng.uniform(0, 120) if clicks else ls.NEVER_DAYS
        last_reply = rng.uniform(0, 120) if replies else ls.NEVER_DAYS
        recent = min(last_click, last_reply)
        mql_z = 1.3 * interest + 0.9 * replies + 0.25 * clicks + (0.6 if company else -0.4) - (0.01 * recent if recent < ls.NEVER_DAYS else 1.5) - 1.0
        mql = int(rng.random() < sig(mql_z))
        sql = int(mql and rng.random() < sig(1.1 * interest + 0.8 * replies + (0.5 if company else -0.6) - 0.8))
        first = rng.uniform(5, 300)
        rows.append({
            "sends": sends, "days_since_first_send": round(first, 1), "days_since_last_send": round(rng.uniform(0, min(first, 60)), 1),
            "clicks": clicks, "click_rate": round(clicks / sends, 4), "days_since_last_click": round(last_click, 1),
            "bounced": int(bounced), "complained": int(rng.random() < 0.01), "company_domain": int(company),
            "role_based": int(rng.random() < (0.15 if company else 0.02)), "has_company": int(company and rng.random() < 0.8),
            "timezone_known": int(rng.random() < 0.7), "mobile_share": round(rng.random() if clicks else 0.0, 4),
            "replies": replies, "days_since_last_reply": round(last_reply, 1),
            **{f"persona_{p}": int(persona == p) for p in ls.PERSONAS},
            "y_reply": int(replies > 0), "y_mql": mql, "y_sql": sql,
            "created_at": pd.Timestamp(base + timedelta(minutes=round(i * 365 * 24 * 60 / n))),
        })
    return pd.DataFrame(rows)


def load_data(force_synthetic: bool, rows: int, seed: int) -> tuple[pd.DataFrame, str, str]:
    if not force_synthetic:
        try:
            real = load_real()
        except Exception as e:
            real, why = pd.DataFrame(), f"could not read Supabase: {e}"
        else:
            why = ""
        enough = len(real) >= MIN_LEADS and all(real[f"y_{t}"].sum() >= MIN_POSITIVES for t in ls.TARGETS) if len(real) else False
        if enough:
            return real, "real", f"{len(real)} real leads"
        counts = ", ".join(f"{t} {int(real[f'y_{t}'].sum())}" for t in ls.TARGETS) if len(real) else "none"
        reason = why or f"{len(real)} leads (need {MIN_LEADS}) with positives {counts} (need {MIN_POSITIVES} each)"
    else:
        reason = "synthetic data requested"
    return synthetic(rows, seed), "synthetic", f"SYNTHETIC demo data: {reason}"


# ---------- Training ----------

def time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("created_at").reset_index(drop=True)
    cut = int(len(df) * (1 - TEST_FRACTION))
    return df.iloc[:cut], df.iloc[cut:]


def _scores(y: np.ndarray, p: np.ndarray) -> dict:
    if len(set(y)) < 2:
        return {"pr_auc": None, "roc_auc": None}
    return {"pr_auc": round(float(average_precision_score(y, p)), 4), "roc_auc": round(float(roc_auc_score(y, p)), 4)}


def train_target(train_df: pd.DataFrame, test_df: pd.DataFrame, target: str) -> tuple[dict, dict]:
    features = ls.FEATURES[target]
    x_train, x_test = train_df[features].astype(float), test_df[features].astype(float)
    y_train, y_test = train_df[f"y_{target}"].to_numpy(), test_df[f"y_{target}"].to_numpy()
    if len(set(y_train)) < 2:
        raise ValueError(f"Training data for {target} needs both outcomes.")

    lr = logistic_model().fit(x_train, y_train)
    gbm_kind, gbm = gbm_model()
    gbm.fit(x_train, y_train)
    lr_scores = _scores(y_test, lr.predict_proba(x_test)[:, 1])
    gbm_scores = _scores(y_test, gbm.predict_proba(x_test)[:, 1])
    # Logistic regression is kept on a tie or when PR-AUC can't be measured: simpler and easier to explain
    gbm_wins = gbm_scores["pr_auc"] is not None and lr_scores["pr_auc"] is not None and gbm_scores["pr_auc"] > lr_scores["pr_auc"]
    winner = ("lightgbm" if gbm_kind == "lightgbm" else "hist_gradient_boosting") if gbm_wins else "logistic_regression"

    # class_weight="balanced" helps the model notice rare outcomes but inflates its probabilities, and the bands
    # (P(SQL) > 0.6 = alert sales) need honest ones. So the winner is calibrated; the uncalibrated fit explains scores.
    explainer = gbm if gbm_wins else lr
    method = "isotonic" if len(train_df) >= CALIBRATION_ISOTONIC_MIN else "sigmoid"
    calibrated = CalibratedClassifierCV(clone(explainer), method=method, cv=CALIBRATION_FOLDS).fit(x_train, y_train)
    p_cal = calibrated.predict_proba(x_test)[:, 1]
    comparison = {
        "features": features, "positives_train": int(y_train.sum()), "positives_test": int(y_test.sum()),
        "logistic_regression": lr_scores, gbm_kind: gbm_scores, "winner": winner,
        "explanations": "shap" if winner == "lightgbm" else "coefficients" if winner == "logistic_regression" else "none",
        "calibration": {
            "method": method,
            "mean_predicted": round(float(p_cal.mean()), 4) if len(p_cal) else None,
            "actual_rate": round(float(y_test.mean()), 4) if len(y_test) else None,
            "brier": round(float(brier_score_loss(y_test, p_cal)), 4) if len(y_test) else None,
        },
    }
    return {"kind": winner, "model": calibrated, "explainer": explainer, "features": features}, comparison


def next_version(out_dir: Path) -> int:
    found = [int(m.group(1)) for p in out_dir.glob("lead_scoring_v*.joblib") if (m := VERSION_RE.match(p.name))]
    return max(found, default=0) + 1


def train(df: pd.DataFrame | None = None, source: str | None = None, note: str = "", out_dir: Path = MODELS_DIR,
          force_synthetic: bool = False, rows: int = 1500, seed: int = 42) -> dict:
    if df is None:
        df, source, note = load_data(force_synthetic, rows, seed)
    synthetic_data = source != "real"
    train_df, test_df = time_split(df)
    targets, comparison = {}, {}
    for t in ls.TARGETS:
        targets[t], comparison[t] = train_target(train_df, test_df, t)

    out_dir.mkdir(parents=True, exist_ok=True)
    version = next_version(out_dir)
    stem = f"lead_scoring_v{version}{'_synthetic' if synthetic_data else ''}"
    meta = {
        "model": "lead_scoring", "version": version, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_source": "synthetic" if synthetic_data else "real",
        "warning": "SYNTHETIC demo data: shows the pipeline works, not how your leads behave." if synthetic_data else None,
        "note": note, "gbm_library": gbm_model()[0],
        "rows": {"total": int(len(df)), "train": int(len(train_df)), "test": int(len(test_df))},
        "split": {"method": "time-based (lead created_at)", "test_fraction": TEST_FRACTION},
        "thresholds": {"sales": ls.SALES_THRESHOLD, "nurture": ls.NURTURE_THRESHOLD},
        "comparison": comparison,
    }
    joblib.dump({"targets": targets}, out_dir / f"{stem}.joblib")
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main():
    parser = argparse.ArgumentParser(description="Train the lead-scoring models.")
    parser.add_argument("--synthetic", action="store_true", help="use the SYNTHETIC demo dataset")
    parser.add_argument("--rows", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=MODELS_DIR)
    args = parser.parse_args()
    meta = train(out_dir=args.out, force_synthetic=args.synthetic, rows=args.rows, seed=args.seed)
    banner = "!!! SYNTHETIC DEMO DATA - not your real leads !!!" if meta["data_source"] == "synthetic" else "Trained on real lead outcomes"
    print(banner)
    print(f"Data: {meta['note']}")
    print(f"Lead scoring v{meta['version']}  (gradient boosting library: {meta['gbm_library']})")
    for t, c in meta["comparison"].items():
        gbm = next(k for k in c if k in ("lightgbm", "hist_gradient_boosting"))
        print(f"  P({t.upper()}): logistic PR-AUC {c['logistic_regression']['pr_auc']} vs {gbm} {c[gbm]['pr_auc']} -> {c['winner']} ({c['explanations']})")
    if meta["data_source"] == "synthetic":
        print(banner)


if __name__ == "__main__":
    main()
