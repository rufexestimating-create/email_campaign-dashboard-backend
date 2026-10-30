"""Train the spam-vs-inbox model: TF-IDF (subject, body) + scaled rule signals -> LogisticRegression -> isotonic calibration.

Run from backend/:
    python -m ml.train                 # real labels if there are enough, otherwise SYNTHETIC demo data
    python -m ml.train --synthetic     # force the synthetic demo set

Writes ml/models/spam_lr_v{N}[_synthetic].joblib, a matching .json with metrics and metadata, and calibration_v{N}[_synthetic].png.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")  # no window: the plot is only saved to a file
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from ml.data import load_training_data
from ml.features import NUMERIC_FEATURES, build_features

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODEL_NAME = "spam_lr"
VERSION_RE = re.compile(rf"^{MODEL_NAME}_v(\d+)(?:_synthetic)?\.joblib$")
INPUT_COLUMNS = ["subject", "body", "content_type"]
TEST_FRACTION = 0.2
CALIBRATION_FOLDS = 3
TOP_COEFFICIENTS = 15


def build_pipeline(min_df: int = 2) -> Pipeline:
    columns = ColumnTransformer([
        ("subject", TfidfVectorizer(ngram_range=(1, 2), min_df=min_df, max_features=2000, sublinear_tf=True), "subject"),
        ("body", TfidfVectorizer(ngram_range=(1, 2), min_df=min_df, max_features=5000, sublinear_tf=True), "body_text"),
        ("numeric", StandardScaler(), NUMERIC_FEATURES),
    ])
    return Pipeline([
        ("features", FunctionTransformer(build_features)),
        ("columns", columns),
        ("clf", LogisticRegression(class_weight="balanced", max_iter=2000)),
    ])


def next_version(models_dir: Path) -> int:
    found = [int(m.group(1)) for p in models_dir.glob(f"{MODEL_NAME}_v*.joblib") if (m := VERSION_RE.match(p.name))]
    return max(found, default=0) + 1


def time_split(df: pd.DataFrame, test_fraction: float = TEST_FRACTION) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Oldest emails train, newest test, the way the model will be used: trained on the past, scoring the future."""
    df = df.sort_values("sent_at").reset_index(drop=True)
    cut = int(len(df) * (1 - test_fraction))
    return df.iloc[:cut], df.iloc[cut:]


def top_coefficients(model: CalibratedClassifierCV, n: int = TOP_COEFFICIENTS) -> dict:
    """Coefficients averaged over the calibration folds (each fold fits its own vocabulary)."""
    totals: dict[str, float] = {}
    folds = model.calibrated_classifiers_
    for cc in folds:
        pipe = cc.estimator
        for name, coef in zip(pipe.named_steps["columns"].get_feature_names_out(), pipe.named_steps["clf"].coef_[0]):
            totals[name] = totals.get(name, 0.0) + coef / len(folds)
    ranked = sorted(totals.items(), key=lambda kv: kv[1])
    fmt = lambda items: [{"feature": k, "coefficient": round(v, 4)} for k, v in items]
    return {"toward_spam": fmt(ranked[::-1][:n]), "toward_inbox": fmt(ranked[:n])}


def evaluate(y: np.ndarray, p: np.ndarray) -> dict:
    metrics = {"test_rows": int(len(y)), "test_spam_rate": round(float(y.mean()), 4) if len(y) else None, "brier": None, "roc_auc": None, "pr_auc": None}
    if len(y):
        metrics["brier"] = round(float(brier_score_loss(y, p)), 4)
    if len(set(y)) == 2:  # AUCs need both classes in the test set
        metrics["roc_auc"] = round(float(roc_auc_score(y, p)), 4)
        metrics["pr_auc"] = round(float(average_precision_score(y, p)), 4)
    return metrics


def save_calibration_plot(y: np.ndarray, p: np.ndarray, path: Path, title: str):
    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.plot([0, 1], [0, 1], linestyle="--", color="#888", label="Perfectly calibrated")
    if len(set(y)) == 2:
        frac, mean = calibration_curve(y, p, n_bins=10, strategy="quantile")
        ax.plot(mean, frac, marker="o", color="#e8590c", label="Model")
    ax.set(xlabel="Predicted probability of spam", ylabel="Observed spam rate", xlim=(0, 1), ylim=(0, 1), title=title)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def train(df: pd.DataFrame | None = None, source: str | None = None, note: str = "", out_dir: Path = MODELS_DIR,
          min_real: int = 200, force_synthetic: bool = False, synthetic_rows: int = 1500, seed: int = 42, min_df: int = 2) -> dict:
    if df is None:
        df, source, note = load_training_data(min_real, force_synthetic, synthetic_rows, seed)
    synthetic = source != "real"
    train_df, test_df = time_split(df)
    if set(train_df["label"]) != {0, 1}:
        raise ValueError("Training data needs both spam (1) and inbox (0) examples.")

    model = CalibratedClassifierCV(build_pipeline(min_df), method="isotonic", cv=CALIBRATION_FOLDS)
    model.fit(train_df[INPUT_COLUMNS], train_df["label"].to_numpy())
    y_test = test_df["label"].to_numpy()
    p_test = model.predict_proba(test_df[INPUT_COLUMNS])[:, 1] if len(test_df) else np.array([])

    out_dir.mkdir(parents=True, exist_ok=True)
    version = next_version(out_dir)
    suffix = "_synthetic" if synthetic else ""
    stem = f"{MODEL_NAME}_v{version}{suffix}"
    model_path, meta_path, plot_path = out_dir / f"{stem}.joblib", out_dir / f"{stem}.json", out_dir / f"calibration_v{version}{suffix}.png"

    save_calibration_plot(y_test, p_test, plot_path, f"Calibration v{version}" + (" (SYNTHETIC DEMO DATA)" if synthetic else ""))
    metadata = {
        "model": MODEL_NAME,
        "version": version,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_source": "synthetic" if synthetic else "real",
        "warning": "SYNTHETIC demo data: these metrics only show the pipeline works, not real inbox placement." if synthetic else None,
        "note": note,
        "label": "spam=1 (spam, complained), inbox=0 (inbox, promotions)",
        "label_sources": {str(k): int(v) for k, v in df["source"].value_counts().items()},
        "rows": {"total": int(len(df)), "train": int(len(train_df)), "test": int(len(test_df)), "train_spam_rate": round(float(train_df["label"].mean()), 4)},
        "split": {"method": "time-based", "test_fraction": TEST_FRACTION, "train_until": str(train_df["sent_at"].max()), "test_from": str(test_df["sent_at"].min()) if len(test_df) else None},
        "metrics": evaluate(y_test, p_test),
        "model_spec": {
            "estimator": "LogisticRegression(class_weight='balanced')",
            "calibration": f"CalibratedClassifierCV(method='isotonic', cv={CALIBRATION_FOLDS})",
            "text_features": "TF-IDF 1-2 grams on subject and plain-text body",
            "numeric_features": NUMERIC_FEATURES,
            "input_columns": INPUT_COLUMNS,
        },
        "top_coefficients": top_coefficients(model),
        "files": {"model": model_path.name, "calibration_plot": plot_path.name},
        "sklearn_version": sklearn.__version__,
    }
    joblib.dump(model, model_path)
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def main():
    parser = argparse.ArgumentParser(description="Train the spam-vs-inbox logistic regression model.")
    parser.add_argument("--synthetic", action="store_true", help="use the SYNTHETIC demo dataset even if real labels exist")
    parser.add_argument("--min-real", type=int, default=200, help="real labels needed before training on them (default 200)")
    parser.add_argument("--rows", type=int, default=1500, help="size of the synthetic dataset (default 1500)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=MODELS_DIR)
    args = parser.parse_args()

    meta = train(out_dir=args.out, min_real=args.min_real, force_synthetic=args.synthetic, synthetic_rows=args.rows, seed=args.seed)
    banner = "!!! SYNTHETIC DEMO DATA - not real inbox placement !!!" if meta["data_source"] == "synthetic" else "Trained on real placement labels"
    m = meta["metrics"]
    print(banner)
    print(f"Data: {meta['note']}")
    print(f"Model v{meta['version']} -> {args.out / meta['files']['model']}")
    print(f"Rows: {meta['rows']['train']} train / {meta['rows']['test']} test (time-based split)")
    print(f"ROC-AUC {m['roc_auc']}  PR-AUC {m['pr_auc']}  Brier {m['brier']}  (test spam rate {m['test_spam_rate']})")
    print("Strongest spam signals: " + ", ".join(c["feature"] for c in meta["top_coefficients"]["toward_spam"][:5]))
    print("Strongest inbox signals: " + ", ".join(c["feature"] for c in meta["top_coefficients"]["toward_inbox"][:5]))
    if meta["data_source"] == "synthetic":
        print(banner)


if __name__ == "__main__":
    main()
