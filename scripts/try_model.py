"""Run the saved scikit-learn spam model on one email and print its prediction (run from backend/).

    python scripts\try_model.py "SUBJECT" "MESSAGE"
    python scripts\try_model.py                      # runs two built-in examples
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from app.services import placement_model as pm

EXAMPLES = [
    ("FREE QUOTE!!! act now", "Get your FREE QUOTE TODAY!! Click https://bit.ly/x and save $500, prices from $99!"),
    ("Ideas for your kitchen remodel",
     "Hi Sam,\n\nWe just finished a kitchen remodel near you. Would a short call this week help?\n\nBest,\nHamza, Arvistry\n\n"
     "Don't want these emails? Reply \"unsubscribe\" and we'll take you off our list.\n1200 Main Street, Austin, TX 78701"),
]


def show(subject: str, body: str):
    r = pm.score(subject, body)
    m = r["model"]
    print("=" * 70)
    print(f"Subject : {subject}")
    print(f"Message : {body[:80]}{'...' if len(body) > 80 else ''}")
    print("-" * 70)
    if m is None:
        print("No trained model found. Run: python -m ml.train")
        return
    print(f"Model file          : {pm.latest_model_path().name}")
    print(f"Model says (spam %) : {m['spam_percent']}%   <- the scikit-learn prediction")
    print(f"Rule checks         : {r['rule_score']}/100 risk")
    print(f"Final spam risk     : {r['score']}/100  ->  inbox score {r['inbox_score']}/100  ({r['label']})")
    if m["reasons"]:
        print("Why (from the model): " + ", ".join(x["label"] for x in m["reasons"]))


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        show(sys.argv[1], sys.argv[2])
    else:
        for subject, body in EXAMPLES:
            show(subject, body)
    print("=" * 70)
