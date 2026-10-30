"""Training data: real placement labels from Supabase, or a clearly marked SYNTHETIC demo set when there aren't enough.

Label rule (placement_labels.placement): spam / complained = 1, inbox / promotions = 0, missing / bounced are skipped
(a bounce says nothing about where a delivered email would have landed).
"""
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.services.spam_checker import UNSUBSCRIBE_LINE
from app.services.spam_knowledge import AUDIENCE_GUIDE, RECOMMENDED_VOCABULARY, builtin_keywords

COLUMNS = ["subject", "body", "content_type", "label", "sent_at", "provider", "source"]
SPAM_PLACEMENTS = {"spam", "complained"}
INBOX_PLACEMENTS = {"inbox", "promotions"}
PAGE = 1000


def label_for(placement: str | None) -> int | None:
    if placement in SPAM_PLACEMENTS:
        return 1
    if placement in INBOX_PLACEMENTS:
        return 0
    return None


# ---------- Real labels ----------

def _all_rows(query) -> list[dict]:
    rows, start = [], 0
    while True:
        page = query.range(start, start + PAGE - 1).execute().data
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        start += PAGE


def _chunks(items: list, size: int = 200):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_real_labels() -> pd.DataFrame:
    """One row per labelled email, joined to what was actually sent (subject from the log, body from the campaign)."""
    from app.db.supabase import get_supabase

    db = get_supabase()
    labels = _all_rows(db.table("placement_labels").select("email_id, campaign_id, recipient, provider, placement, source, observed_at").order("observed_at"))
    labels = [l for l in labels if label_for(l["placement"]) is not None]
    if not labels:
        return pd.DataFrame(columns=COLUMNS)

    logs: dict[str, dict] = {}
    email_ids = sorted({l["email_id"] for l in labels if l["email_id"]})
    for chunk in _chunks(email_ids):
        for row in db.table("email_logs").select("id, recipient, subject, snippet, campaign_id, variant, sent_at").in_("id", chunk).execute().data:
            logs[row["id"]] = row
    # Labels recorded without an email id: match the campaign's log for that recipient (latest send wins)
    by_campaign_recipient: dict[tuple[str, str], dict] = {}
    loose_campaigns = sorted({l["campaign_id"] for l in labels if not l["email_id"] and l["campaign_id"]})
    for chunk in _chunks(loose_campaigns):
        for row in db.table("email_logs").select("id, recipient, subject, snippet, campaign_id, variant, sent_at").in_("campaign_id", chunk).order("sent_at").execute().data:
            by_campaign_recipient[(row["campaign_id"], row["recipient"].lower())] = row

    campaign_ids = sorted({c for c in [l["campaign_id"] for l in labels] + [g["campaign_id"] for g in logs.values()] if c})
    campaigns: dict[str, dict] = {}
    for chunk in _chunks(campaign_ids):
        for row in db.table("campaigns").select("id, subject_a, body_a, subject_b, body_b, content_type").in_("id", chunk).execute().data:
            campaigns[row["id"]] = row

    out = []
    for l in labels:
        log = logs.get(l["email_id"]) if l["email_id"] else by_campaign_recipient.get((l["campaign_id"], (l["recipient"] or "").lower()))
        campaign = campaigns.get((log or {}).get("campaign_id") or l["campaign_id"])
        variant_b = (log or {}).get("variant") == "B"
        subject = (log or {}).get("subject") or (campaign and campaign["subject_b" if variant_b else "subject_a"])
        body = (campaign and campaign["body_b" if variant_b else "body_a"]) or (log or {}).get("snippet")
        if not subject or not body:
            continue  # can't rebuild what was sent
        out.append({
            "subject": subject, "body": body, "content_type": (campaign or {}).get("content_type") or "text",
            "label": label_for(l["placement"]), "sent_at": pd.Timestamp((log or {}).get("sent_at") or l["observed_at"]),
            "provider": l["provider"], "source": l["source"],
        })
    return pd.DataFrame(out, columns=COLUMNS)


# ---------- SYNTHETIC demo data ----------
# Made-up emails so the pipeline can be built and tested before real seed-test labels exist.
# A model trained on this only re-learns these hand-written patterns; it is NOT evidence of real inbox placement.

SYNTHETIC_BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEMO_ADDRESS = "Arvistry Design Studio, 1200 Example Street, Suite 300, Austin, TX 78701"
CLEAN_SUBJECTS = [g["good_subject"] for g in AUDIENCE_GUIDE] + [
    "{{first_name}}, ideas for your kitchen remodel",
    "Bathroom remodel timeline for {{company}}",
    "Following up on the {{project}} site visit",
    "Concept drawings for your home addition",
    "Questions about your renovation brief",
    "Case study: a 1960s ranch house, reworked",
    "Permit drawings ready for review",
    "Next steps on your bathroom layout",
    "{{first_name}}, a quick question about scheduling",
    "Coordinating trades for the {{project}} remodel",
]
CLEAN_SENTENCES = [
    "I'm writing because {{company}} is planning work on {{project}}, and we have recently finished something similar nearby.",
    "We put together a short {v} that shows how we approached a comparable remodel.",
    "Our team handles the {v} and the {v}, so your contractor works from one coordinated set of drawings.",
    "If it's useful, I can share the {v} from our last bathroom project in your area.",
    "We usually start with a {v} so the scope and budget are clear before anything is built.",
    "Would a 15-minute call next Tuesday or Wednesday work to talk through your {v}?",
    "Happy to send over our {v} if you'd like to see more of our residential work.",
    "Our architects can review the {v} and point out any permit issues early.",
]
SPAM_SUBJECT_FORMS = ["{P}!!!", "{p} - act now", "RE: {p}", "{P} INSIDE", "Don't miss this: {p}!", "{p} for {{first_name}} $$$", "{p}"]
SPAM_SENTENCES = [
    "Get your {p} TODAY before this offer EXPIRES!!",
    "This is a LIMITED TIME deal, {p} for the first 10 homeowners only!!!",
    "Click here for your {p}: {link}",
    "{P}! Save $500 on your remodel, prices from $99!",
    "We guarantee the best results, {p}, no catch.",
    "Reply now to claim {p} - spots are going FAST!",
]
SHORT_LINKS = ["https://bit.ly/3xRemodel", "https://tinyurl.com/freequote22", "https://cutt.ly/deal7"]
OWN_LINKS = ["https://arvistry.com/projects/bathroom-remodel", "https://arvistry.com/case-studies/kitchen", "https://arvistry.com/portfolio"]


def _clean_email(rng: random.Random) -> tuple[str, str]:
    subject = rng.choice(CLEAN_SUBJECTS)
    lines = ["Hi {{first_name}},", ""]
    for s in rng.sample(CLEAN_SENTENCES, rng.randint(2, 4)):
        lines.append(s.replace("{v}", rng.choice(RECOMMENDED_VOCABULARY)))
    if rng.random() < 0.5:
        lines.append(f"You can see the project here: {rng.choice(OWN_LINKS)}")
    lines += ["", "Best regards,", "Hamza, Arvistry"]
    if rng.random() < 0.9:
        lines += ["", UNSUBSCRIBE_LINE]
    if rng.random() < 0.85:
        lines.append(DEMO_ADDRESS)
    return subject, "\n".join(lines)


def _spam_email(rng: random.Random, phrases: list[str]) -> tuple[str, str]:
    p = rng.choice(phrases)
    subject = rng.choice(SPAM_SUBJECT_FORMS).replace("{P}", p.upper()).replace("{p}", p)
    lines = ["Dear Homeowner,", ""]
    for s in rng.sample(SPAM_SENTENCES, rng.randint(2, 4)):
        q = rng.choice(phrases)
        link = rng.choice(SHORT_LINKS if rng.random() < 0.6 else OWN_LINKS)
        lines.append(s.replace("{P}", q.upper()).replace("{p}", q).replace("{link}", link))
    if rng.random() < 0.4:
        lines += ["", UNSUBSCRIBE_LINE]
    if rng.random() < 0.2:
        lines.append(DEMO_ADDRESS)
    return subject, "\n".join(lines)


# Several framings for the one-phrase variants, so the model can't learn a single fixed sentence as the signal
PHRASE_FRAMES = ["{p}.", "Ask us about {p}.", "This includes {p}.", "Note: {p} applies.", "{p}, as discussed.", "Mention {p} when you reply."]


def _with_phrase(rng: random.Random, body: str, phrase: str) -> str:
    lines = body.split("\n")
    at = rng.randint(2, max(2, len(lines) - 4))  # somewhere after the greeting, before the sign-off
    lines.insert(at, rng.choice(PHRASE_FRAMES).replace("{p}", phrase).capitalize())
    return "\n".join(lines)


def _mild_spam_email(rng: random.Random, phrases: list[str]) -> tuple[str, str]:
    """Spam-labelled but polite-looking: keeps the problem from being trivially separable."""
    subject, body = _clean_email(rng)
    return subject, _with_phrase(rng, body, rng.choice(phrases)).replace(UNSUBSCRIBE_LINE, "")


def _mild_clean_email(rng: random.Random, phrases: list[str]) -> tuple[str, str]:
    """Inbox-labelled but with one trigger phrase, like real professional emails sometimes have."""
    subject, body = _clean_email(rng)
    return subject, _with_phrase(rng, body, rng.choice(phrases))


def synthetic_dataset(n: int = 1500, seed: int = 42, spam_rate: float = 0.3, label_noise: float = 0.05) -> pd.DataFrame:
    rng = random.Random(seed)
    keywords = builtin_keywords()
    strong = [k["phrase"] for k in keywords if k["severity"] in ("high", "medium") and k["phrase"].replace(" ", "").isalnum()]
    weak = [k["phrase"] for k in keywords if k["severity"] in ("medium", "low") and k["phrase"].replace(" ", "").isalnum()]
    rows = []
    for i in range(n):
        is_spam = rng.random() < spam_rate
        if is_spam:
            subject, body = _mild_spam_email(rng, weak) if rng.random() < 0.2 else _spam_email(rng, strong)
        else:
            subject, body = _mild_clean_email(rng, weak) if rng.random() < 0.15 else _clean_email(rng)
        label = int(is_spam) ^ int(rng.random() < label_noise)
        rows.append({
            "subject": subject, "body": body, "content_type": "text", "label": label,
            "sent_at": pd.Timestamp(SYNTHETIC_BASE_DATE + timedelta(minutes=round(i * 180 * 24 * 60 / n))),
            "provider": rng.choice(["gmail", "outlook", "yahoo", "icloud", "other"]), "source": "SYNTHETIC",
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def load_training_data(min_real: int = 200, force_synthetic: bool = False, synthetic_rows: int = 1500, seed: int = 42) -> tuple[pd.DataFrame, str, str]:
    """Returns (data, "real" | "synthetic", note)."""
    if not force_synthetic:
        try:
            real = load_real_labels()
        except Exception as e:  # no database connection: fall back rather than fail
            real, error = pd.DataFrame(columns=COLUMNS), f" (could not read Supabase: {e})"
        else:
            error = ""
        classes = set(real["label"]) if len(real) else set()
        if len(real) >= min_real and classes == {0, 1}:
            return real, "real", f"{len(real)} real placement labels"
        reason = f"only {len(real)} usable real labels{error}, need {min_real} with both spam and inbox examples"
    else:
        reason = "synthetic data requested"
    return synthetic_dataset(synthetic_rows, seed), "synthetic", f"SYNTHETIC demo data: {reason}"
