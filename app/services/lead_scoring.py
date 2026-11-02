"""Lead scoring (M9): P(reply), P(MQL), P(SQL) per lead, RFM / K-Means segments and action bands.

Features come only from what you can see before the outcome: sends, clicks, bounces, persona, address type and
timing. P(reply) never sees replies (that would be the answer); P(MQL) and P(SQL) may, because a reply comes before
a lead is qualified. Bands follow P(SQL): above 0.6 = alert sales, 0.3-0.6 = nurture, below 0.3 = awareness.

Models are trained by ml/lead_train.py. Logistic regression is compared with LightGBM on PR-AUC and the better one is
kept per target; SHAP explanations (LightGBM's exact TreeSHAP, pred_contrib) are only produced when LightGBM wins.
Without a saved model, nothing is scored (segments still work, they don't need a model).
"""
import json
import logging
import math
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.services.event_tracking import provider_for
from app.services.spam_knowledge import ROLE_BASED_PREFIXES

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "ml" / "models"
MODEL_FILE_RE = re.compile(r"^lead_scoring_v(\d+)(_synthetic)?\.joblib$")
NEVER_DAYS = 365  # "never happened" is treated as a year ago, so the number stays comparable
SALES_THRESHOLD = 0.6
NURTURE_THRESHOLD = 0.3
KMEANS_MIN_ENGAGED = 20
SEGMENT_ORDER = ["Champions", "Engaged", "Cooling", "Cold"]
TARGETS = ("reply", "mql", "sql")
PERSONAS = ("contractor", "architect", "homeowner", "other")
QUALIFIED = {"mql": ("mql", "sql", "customer"), "sql": ("sql", "customer")}

BASE_FEATURES = [
    "sends", "days_since_first_send", "days_since_last_send", "clicks", "click_rate", "days_since_last_click",
    "bounced", "complained", "company_domain", "role_based", "has_company", "timezone_known", "mobile_share",
] + [f"persona_{p}" for p in PERSONAS]
REPLY_FEATURES = ["replies", "days_since_last_reply"]
FEATURES = {"reply": BASE_FEATURES, "mql": BASE_FEATURES + REPLY_FEATURES, "sql": BASE_FEATURES + REPLY_FEATURES}

FEATURE_LABELS = {
    "sends": "Emails received", "days_since_first_send": "Contact age (days)", "days_since_last_send": "Days since last email",
    "clicks": "Link clicks", "click_rate": "Click rate", "days_since_last_click": "Days since last click", "bounced": "Bounced",
    "complained": "Marked as spam", "company_domain": "Company email address", "role_based": "Generic inbox (info@…)",
    "has_company": "Company known", "timezone_known": "Location known", "mobile_share": "Reads on mobile",
    "replies": "Replies", "days_since_last_reply": "Days since last reply",
    **{f"persona_{p}": f"Persona: {p}" for p in PERSONAS},
}


# ---------- Features ----------

def _utc(value) -> datetime:
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _days(now: datetime, when: datetime | None) -> float:
    return min(NEVER_DAYS, max(0.0, (now - when).total_seconds() / 86400)) if when else NEVER_DAYS


def lead_features(lead: dict, sends: list[dict], events: list[dict], now: datetime) -> dict:
    """One lead's feature row, from the emails sent to them and the events on those emails."""
    sent_times = [_utc(s["sent_at"]) for s in sends if s.get("status") in ("sent", "simulated")]
    by_type: dict[str, list[datetime]] = defaultdict(list)
    devices = Counter()
    for e in events:
        by_type[e["event_type"]].append(_utc(e["occurred_at"]))
        if e["event_type"] in ("clicked", "opened") and e.get("device") in ("desktop", "mobile", "tablet"):
            devices[e["device"]] += 1
    clicks, replies = len(by_type["clicked"]), len(by_type["replied"])
    email = lead["email"].lower()
    persona = lead.get("persona") or "other"
    known_devices = sum(devices.values())
    return {
        "sends": len(sent_times),
        "days_since_first_send": _days(now, min(sent_times, default=None)),
        "days_since_last_send": _days(now, max(sent_times, default=None)),
        "clicks": clicks,
        "click_rate": round(clicks / len(sent_times), 4) if sent_times else 0.0,
        "days_since_last_click": _days(now, max(by_type["clicked"], default=None)),
        "bounced": int(bool(by_type["bounced"])),
        "complained": int(bool(by_type["complained"])),
        "company_domain": int(provider_for(email) == "other"),
        "role_based": int(email.split("@")[0] in ROLE_BASED_PREFIXES),
        "has_company": int(bool(lead.get("company"))),
        "timezone_known": int(bool(lead.get("timezone"))),
        "mobile_share": round((devices["mobile"] + devices["tablet"]) / known_devices, 4) if known_devices else 0.0,
        "replies": replies,
        "days_since_last_reply": _days(now, max(by_type["replied"], default=None)),
        **{f"persona_{p}": int(persona == p) for p in PERSONAS},
    }


def labels_for(lead: dict, features: dict) -> dict:
    stage = lead.get("lifecycle_stage") or "new"
    return {"reply": int(features["replies"] > 0), "mql": int(stage in QUALIFIED["mql"]), "sql": int(stage in QUALIFIED["sql"])}


def build_rows(leads: list[dict], logs: list[dict], events: list[dict], now: datetime | None = None) -> list[dict]:
    """Feature rows for every lead (one user's data)."""
    now = now or datetime.now(timezone.utc)
    sends_by, events_by = defaultdict(list), defaultdict(list)
    for s in logs:
        sends_by[s["recipient"].lower()].append(s)
    for e in events:
        events_by[e["recipient"].lower()].append(e)
    rows = []
    for lead in leads:
        email = lead["email"].lower()
        features = lead_features(lead, sends_by[email], events_by[email], now)
        rows.append({"lead": lead, "features": features, "labels": labels_for(lead, features)})
    return rows


# ---------- RFM segments ----------

def rfm(features: dict) -> tuple[float, int, int]:
    """Recency (days since last click or reply), frequency (clicks + replies), value (clicks + 3 x replies)."""
    recency = min(features["days_since_last_click"], features["days_since_last_reply"])
    return recency, features["clicks"] + features["replies"], features["clicks"] + 3 * features["replies"]


def rule_segment(features: dict) -> str:
    recency, frequency, _ = rfm(features)
    if features["sends"] == 0:
        return "New"
    if frequency == 0:
        return "Cold"
    if recency <= 14 and features["replies"] > 0:
        return "Champions"
    if recency <= 30:
        return "Engaged"
    return "Cooling"


def segment_all(feature_rows: list[dict]) -> tuple[list[str], str]:
    """K-Means (k=4) on log-scaled RFM once enough leads engage; clear rules before that. Returns (segments, method)."""
    engaged = sum(rfm(f)[1] > 0 for f in feature_rows)
    if engaged < KMEANS_MIN_ENGAGED:
        return [rule_segment(f) for f in feature_rows], "rules"
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    x = np.array([[-math.log1p(r), math.log1p(fq), math.log1p(v)] for r, fq, v in (rfm(f) for f in feature_rows)])
    scaled = StandardScaler().fit_transform(x)
    km = KMeans(n_clusters=min(4, len(feature_rows)), n_init=10, random_state=0).fit(scaled)
    # Best centroid (recent, frequent, valuable) gets the first name
    order = np.argsort(-km.cluster_centers_.sum(axis=1))
    names = {int(cluster): SEGMENT_ORDER[rank] for rank, cluster in enumerate(order)}
    return [names[int(c)] if f["sends"] else "New" for c, f in zip(km.labels_, feature_rows)], "kmeans"


def band_for(p_sql: float | None) -> str | None:
    if p_sql is None:
        return None
    return "sales" if p_sql > SALES_THRESHOLD else "nurture" if p_sql >= NURTURE_THRESHOLD else "awareness"


# ---------- Models ----------

_lock = threading.Lock()
_cache: dict = {"key": None, "bundle": None}


def latest_model_path(models_dir: Path | None = None) -> Path | None:
    found = [(int(m.group(1)), p) for p in (models_dir or MODELS_DIR).glob("lead_scoring_v*.joblib") if (m := MODEL_FILE_RE.match(p.name))]
    return max(found)[1] if found else None


def load_bundle(models_dir: Path | None = None) -> dict | None:
    path = latest_model_path(models_dir)
    if path is None:
        return None
    key = (str(path), path.stat().st_mtime)
    with _lock:
        if _cache["key"] != key:
            import joblib
            import sklearn  # noqa: F401  loads the OpenMP runtime (vcomp140.dll) that LightGBM needs on Windows without the VC++ redistributable

            bundle = joblib.load(path)
            bundle["meta"] = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            _cache.update(key=key, bundle=bundle)
        return _cache["bundle"]


def _explain(target: dict, x) -> list[list[dict]]:
    """Top 3 drivers toward a positive outcome for each row: SHAP for LightGBM, coefficient x scaled value for logistic regression."""
    import numpy as np

    features = target["features"]
    # Probabilities come from the calibrated model; drivers from the uncalibrated fit (calibration doesn't change which features matter)
    fitted = target.get("explainer", target["model"])
    if target["kind"] == "lightgbm":
        contrib = np.asarray(fitted.predict(x, pred_contrib=True))[:, :-1]  # last column is the bias
        method = "shap"
    elif target["kind"] == "logistic_regression":
        pipe = fitted
        contrib = pipe.named_steps["scale"].transform(x) * pipe.named_steps["clf"].coef_[0]
        method = "coefficient"
    else:
        return [[] for _ in range(len(x))]
    out = []
    for row in contrib:
        top = sorted(((features[i], float(v)) for i, v in enumerate(row) if v > 0), key=lambda kv: -kv[1])[:3]
        out.append([{"feature": f, "label": FEATURE_LABELS.get(f, f), "impact": round(v, 3), "method": method} for f, v in top])
    return out


def predict(bundle: dict, feature_rows: list[dict]) -> list[dict]:
    """{"p_reply", "p_mql", "p_sql", "drivers": {target: [...]}} per row."""
    import pandas as pd

    out = [{"drivers": {}} for _ in feature_rows]
    for name in TARGETS:
        target = bundle["targets"][name]
        x = pd.DataFrame(feature_rows, columns=target["features"]).astype(float)
        probs = target["model"].predict_proba(x)[:, 1]
        drivers = _explain(target, x)
        for i, p in enumerate(probs):
            out[i][f"p_{name}"] = round(float(p), 4)
            out[i]["drivers"][name] = drivers[i]
    return out


# ---------- Loading and scoring a user's leads ----------

def _pages(query, limit: int = 50_000) -> list[dict]:
    rows, start = [], 0
    while len(rows) < limit:
        page = query.range(start, start + 999).execute().data
        rows.extend(page)
        if len(page) < 1000:
            break
        start += 1000
    return rows


def load_user_data(user_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    from app.db.supabase import get_supabase

    db = get_supabase()
    leads = _pages(db.table("leads").select("*").eq("user_id", user_id).order("created_at"))
    logs = _pages(db.table("email_logs").select("recipient, status, sent_at").eq("user_id", user_id))
    events = _pages(db.table("email_events").select("recipient, event_type, occurred_at, device").eq("user_id", user_id)
                    .in_("event_type", ["opened", "clicked", "replied", "bounced", "complained"]))
    return leads, logs, events


def backfill_leads(user_id: str, leads: list[dict], logs: list[dict]) -> int:
    """Everyone you've emailed becomes a lead, so people mailed before lead tracking existed are scored too."""
    from app.db.supabase import get_supabase

    from app.services.warmup import seed_emails

    known = {l["email"].lower() for l in leads} | seed_emails(user_id)  # seed inboxes are test addresses, not leads
    missing = sorted({s["recipient"].lower() for s in logs} - known)
    for i in range(0, len(missing), 500):
        get_supabase().table("leads").upsert([{"user_id": user_id, "email": e} for e in missing[i:i + 500]],
                                             on_conflict="user_id,email", ignore_duplicates=True).execute()
    return len(missing)


def score_user(user_id: str, now: datetime | None = None) -> dict:
    from app.db.supabase import get_supabase

    now = now or datetime.now(timezone.utc)
    leads, logs, events = load_user_data(user_id)
    added = backfill_leads(user_id, leads, logs)
    if added:
        leads, logs, events = load_user_data(user_id)
    rows = build_rows(leads, logs, events, now)
    feature_rows = [r["features"] for r in rows]
    segments, segment_method = segment_all(feature_rows) if rows else ([], "rules")
    bundle = load_bundle()
    preds = predict(bundle, feature_rows) if bundle and rows else [{} for _ in rows]
    version = f"v{bundle['meta']['version']}{'-synthetic' if bundle['meta']['data_source'] == 'synthetic' else ''}" if bundle else None

    db = get_supabase()
    updates = []
    for r, seg, p in zip(rows, segments, preds):
        updates.append({
            "id": r["lead"]["id"], "user_id": user_id, "email": r["lead"]["email"],
            "p_reply": p.get("p_reply"), "p_mql": p.get("p_mql"), "p_sql": p.get("p_sql"),
            "score_band": band_for(p.get("p_sql")), "segment": seg, "scored_at": now.isoformat(), "score_version": version,
        })
    for i in range(0, len(updates), 500):
        db.table("leads").upsert(updates[i:i + 500], on_conflict="id").execute()
    return {"scored": len(updates), "leads_added": added, "model_version": version, "segment_method": segment_method}


# How far a contact has got with your emails, best first. A bounce or complaint only wins when nothing better happened.
ENGAGEMENT_RANK = {"replied": 5, "clicked": 4, "opened": 3, "delivered": 2, "sent": 1}


def lead_activity(logs: list[dict]) -> dict[str, dict]:
    """Per contact (lower-cased email): emails sent, the last send, and their best engagement across all of them."""
    out: dict[str, dict] = {}
    for l in logs:
        if l["status"] != "sent":
            continue
        a = out.setdefault(l["recipient"].lower(), {"emails_sent": 0, "last_sent_at": None, "best": None, "problem": None})
        a["emails_sent"] += 1
        if not a["last_sent_at"] or l["sent_at"] > a["last_sent_at"]:
            a["last_sent_at"] = l["sent_at"]
        event = l.get("last_event") or "sent"
        if event in ("bounced", "complained"):
            a["problem"] = "complained" if event == "complained" or a["problem"] == "complained" else "bounced"
        elif ENGAGEMENT_RANK.get(event, 1) > ENGAGEMENT_RANK.get(a["best"] or "", 0):
            a["best"] = event
    return {
        email: {"emails_sent": a["emails_sent"], "last_sent_at": a["last_sent_at"],
                "engagement": a["problem"] if a["problem"] and ENGAGEMENT_RANK.get(a["best"] or "", 0) < 3 else a["best"] or "sent"}
        for email, a in out.items()
    }


def scores_overview(user_id: str) -> dict:
    """Every lead for the UI, with its activity (emails sent, last send, best engagement) and next automatic follow-up,
    most recently emailed first;
    plus the stored scores, counts per band and segment, and the sales alerts."""
    from app.db.supabase import get_supabase

    db = get_supabase()
    leads = _pages(db.table("leads").select(
        "id, email, name, company, persona, lifecycle_stage, p_reply, p_mql, p_sql, score_band, segment, scored_at, score_version, created_at"
    ).eq("user_id", user_id).order("created_at"))
    activity = lead_activity(_pages(db.table("email_logs").select("recipient, status, last_event, sent_at").eq("user_id", user_id)))
    none = {"emails_sent": 0, "last_sent_at": None, "engagement": "none"}
    # When each lead's next automatic follow-up is due, so the stage picker shows what it is turning on or off
    next_follow_up: dict[str, str] = {}
    for f in _pages(db.table("follow_up_sends").select("recipient, due_at").eq("user_id", user_id).eq("status", "pending").order("due_at")):
        next_follow_up.setdefault(f["recipient"].lower(), f["due_at"])
    for l in leads:
        l.update(activity.get(l["email"].lower(), none))
        l["next_follow_up_at"] = next_follow_up.get(l["email"].lower())
    leads.sort(key=lambda l: l["last_sent_at"] or "", reverse=True)
    bundle = load_bundle()
    meta = bundle["meta"] if bundle else None
    return {
        "leads": leads,
        "bands": dict(Counter(l["score_band"] or "unscored" for l in leads)),
        "segments": dict(Counter(l["segment"] or "unscored" for l in leads)),
        "alerts": [l for l in leads if l["score_band"] == "sales" and l["lifecycle_stage"] not in ("sql", "customer", "lost")],
        "thresholds": {"sales": SALES_THRESHOLD, "nurture": NURTURE_THRESHOLD},
        "model": {k: meta[k] for k in ("version", "data_source", "trained_at", "comparison", "warning", "gbm_library")} if meta else None,
        "last_scored": max((l["scored_at"] for l in leads if l["scored_at"]), default=None),
    }
