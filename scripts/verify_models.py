"""Independent checks of the saved scikit-learn models on data they never saw (run from backend/)."""
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from app.services import lead_scoring as ls
from app.services import placement_model as pm
from ml.data import synthetic_dataset
from ml.features import build_features
from ml.lead_train import synthetic as synthetic_leads

ok = True


def check(name, passed, detail=""):
    global ok
    ok &= bool(passed)
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")


def ece(y, p, bins=10):
    """Expected calibration error: average gap between predicted and observed rates."""
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, bins - 1)
    return sum(abs(p[idx == b].mean() - y[idx == b].mean()) * (idx == b).mean() for b in range(bins) if (idx == b).any())


print("SPAM / INBOX MODEL")
loaded = pm.load_model()
check("latest model file loads", loaded is not None, pm.latest_model_path().name)
model = loaded["estimator"]
fresh = synthetic_dataset(1000, seed=2026)  # different seed from training (42): unseen emails
X = fresh[["subject", "body", "content_type"]]
y = fresh["label"].to_numpy()
p = model.predict_proba(X)[:, 1]
check("probabilities are valid (0-1, no NaN)", np.isfinite(p).all() and (p >= 0).all() and (p <= 1).all())
roc, pr, brier, cal = roc_auc_score(y, p), average_precision_score(y, p), brier_score_loss(y, p), ece(y, p)
check("ranks spam above inbox on unseen emails (ROC-AUC > 0.8)", roc > 0.8, f"{roc:.3f}")
check("finds spam well above chance (PR-AUC > 2x base rate)", pr > 2 * y.mean(), f"{pr:.3f} vs base {y.mean():.3f}")
check("probabilities are calibrated (ECE < 0.08)", cal < 0.08, f"ECE {cal:.3f}, Brier {brier:.3f}")

rule = build_features(X)  # rules-only baseline: kw counts + issues as a crude score
rule_score = (rule["kw_high"] * 3 + rule["kw_medium"] * 2 + rule["kw_low"] + rule.filter(like="issue_").sum(axis=1)
              + (1 - rule["has_unsubscribe"])).to_numpy(dtype=float)
base_roc = roc_auc_score(y, rule_score)
check("model at least matches a simple rules-only score", roc >= base_roc - 0.02, f"model {roc:.3f} vs rules {base_roc:.3f}")

spam = pm.score("FREE QUOTE!!! act now", "Get your FREE QUOTE TODAY!! https://bit.ly/x save $500, from $99!")
clean = pm.score("Concept drawings for your bathroom remodel",
                 "Hi Sam,\n\nWe put together a short case study of a similar remodel. Would a call on Tuesday work?\n\nBest,\nHamza\n\n"
                 "Don't want these emails? Reply \"unsubscribe\".\n1200 Example Street, Austin, TX 78701")
check("obvious spam scores high, clean email scores low", spam["score"] >= 45 > 20 > clean["score"], f"spam {spam['score']}, clean {clean['score']}")
check("reasons explain the spam email", len(spam["model"]["reasons"]) > 0, ", ".join(r["label"] for r in spam["model"]["reasons"][:3]))
same = [pm.score("Hello there", "A quick note about your remodel.")["score"] for _ in range(3)]
check("same email always gets the same score", len(set(same)) == 1, str(same[0]))

print("\nLEAD SCORING MODELS")
bundle = ls.load_bundle()
check("latest lead model loads", bundle is not None, f"v{bundle['meta']['version']} ({bundle['meta']['data_source']})")
leads = synthetic_leads(1200, seed=2026)  # unseen leads
rows = leads[ls.BASE_FEATURES + ls.REPLY_FEATURES].to_dict("records")
preds = ls.predict(bundle, rows)
for t in ls.TARGETS:
    yt = leads[f"y_{t}"].to_numpy()
    pt = np.array([r[f"p_{t}"] for r in preds])
    check(f"P({t}) valid and better than chance on unseen leads", np.isfinite(pt).all() and roc_auc_score(yt, pt) > 0.7 and average_precision_score(yt, pt) > yt.mean(),
          f"ROC-AUC {roc_auc_score(yt, pt):.3f}, PR-AUC {average_precision_score(yt, pt):.3f} vs base {yt.mean():.3f}")
p_mql = np.array([r["p_mql"] for r in preds])
p_sql = np.array([r["p_sql"] for r in preds])
check("SQL is rarer than MQL on average (as in reality)", p_sql.mean() <= p_mql.mean() + 0.05, f"mean P(MQL) {p_mql.mean():.2f}, P(SQL) {p_sql.mean():.2f}")

base = {k: 0 for k in ls.BASE_FEATURES + ls.REPLY_FEATURES}
base.update(sends=5, days_since_first_send=60, days_since_last_send=5, days_since_last_click=ls.NEVER_DAYS,
            days_since_last_reply=ls.NEVER_DAYS, company_domain=1, has_company=1, persona_contractor=1)
engaged = {**base, "clicks": 3, "click_rate": 0.6, "days_since_last_click": 3, "replies": 2, "days_since_last_reply": 4}
cold, hot = ls.predict(bundle, [base, engaged])
check("an engaged lead scores higher than a silent one", hot["p_sql"] > cold["p_sql"] and hot["p_reply"] > cold["p_reply"],
      f"P(SQL) {cold['p_sql']:.2f} -> {hot['p_sql']:.2f}")
check("bands follow the thresholds", ls.band_for(0.7) == "sales" and ls.band_for(0.45) == "nurture" and ls.band_for(0.1) == "awareness")
winners = {t: c["winner"] for t, c in bundle["meta"]["comparison"].items()}
check("winner per target was chosen by PR-AUC", all(
    (c["winner"] == "logistic_regression") == ((c["logistic_regression"]["pr_auc"] or 0) >= (c.get("lightgbm") or c.get("hist_gradient_boosting") or {}).get("pr_auc", 0) or 0)
    for c in bundle["meta"]["comparison"].values()), str(winners))

print("\nRESULT:", "ALL MODEL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
