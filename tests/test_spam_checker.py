"""Unit tests for the rules-based spam scorer. Run from backend/: python -m pytest"""
from app.services.spam_checker import analyze

CLEAN_BODY = (
    "Hi {{first_name}},\n\n"
    "We finished a bathroom remodel for a family in Austin last month, from layout drawings to tiling, in six weeks. "
    "I put together a short case study with photos and the project timeline that shows how we kept the work on budget.\n\n"
    "Would a 15-minute call next week be useful to talk about your plans?\n\n"
    "Best regards,\nSam\n"
    "Arvistry, 120 Main Street, Austin, TX 78701\n"
    "Don't want these emails? Reply \"unsubscribe\" and we'll take you off our list."
)


def apply_fix(subject: str, body: str, fix: dict) -> tuple[str, str]:
    """Applies a fix the way the editor will: edits arrive last-to-first per field."""
    texts = {"subject": subject, "body": body}
    for e in fix["edits"]:
        t = texts[e["field"]]
        texts[e["field"]] = t[:e["start"]] + e["text"] + t[e["end"]:]
    return texts["subject"], texts["body"]


def issue(result: dict, id_: str) -> dict | None:
    return next((i for i in result["issues"] if i["id"] == id_), None)


def test_clean_email_scores_high():
    r = analyze("Bathroom remodel case study for {{company}}", CLEAN_BODY)
    assert r["level"] == "good"
    assert r["risk"] == "low"
    assert r["inbox_score"] >= 80
    assert r["inbox_score"] == 100 - r["score"]
    assert r["stats"]["has_unsubscribe"] and r["stats"]["has_address"]


def test_spammy_email_scores_low():
    r = analyze("FREE MONEY!!! ACT NOW", "CLICK HERE to get FREE CASH $$$ guaranteed!!! http://bit.ly/x")
    assert r["level"] == "poor"
    assert r["risk"] == "high"
    assert r["inbox_score"] < 50
    assert 1 <= len(r["reasons"]) <= 5
    assert r["reasons"] == sorted(r["reasons"], key=lambda x: -x["points"])


def test_keyword_positions_point_into_raw_text():
    body = "Hello {{name}}, this is a free consultation offer."
    r = analyze("Project update", body)
    hit = next(k for k in r["keywords"] if k["location"] == "body")
    span = hit["spans"][0]
    assert body[span["start"]:span["end"]].lower() == hit["matched"].lower()


def test_placeholders_are_not_scanned():
    r = analyze("Hello", "Hi {{free_gift}}, here is our project portfolio.")
    assert not any("free" in k["matched"].lower() for k in r["keywords"])


def test_html_tags_are_not_scanned_and_fixes_keep_them():
    body = '<p>Get a <b>free</b> estimate today</p><img alt="free gift" src="https://example.com/a.png">'
    r = analyze("Your estimate", body, "html")
    kw = [h for h in r["highlights"] if h["ref"].startswith("kw:")]
    assert kw and all(h["start"] < body.index("<img") for h in kw)  # nothing matched inside the <img> tag
    for k in r["keywords"]:
        _, fixed = apply_fix("", body, k["fix"])
        assert fixed.count("<b>") == fixed.count("</b>") == 1


def test_subject_caps_fix_keeps_placeholders_and_acronyms():
    subject = "BIG HVAC NEWS FOR {{company}}"
    r = analyze(subject, CLEAN_BODY)
    new_subject, _ = apply_fix(subject, CLEAN_BODY, issue(r, "subject_caps")["fix"])
    assert new_subject == "Big HVAC news for {{company}}"


def test_exclamation_and_dollar_counts():
    r = analyze("Hello there friend!", "Prices from $500, $900 and $1,200!! Call us!")
    assert issue(r, "subject_exclaim_one")
    assert issue(r, "body_multi_exclaim")
    assert issue(r, "money_signs")
    assert r["stats"]["money_signs"] == 3
    _, body = apply_fix("", "Prices from $500, $900 and $1,200!! Call us!", issue(r, "body_multi_exclaim")["fix"])
    assert "!!" not in body


def test_caps_ratio_and_body_caps_fix():
    body = "Our NEW kitchen OFFER is AMAZING and the TEAM is ready. " * 2
    r = analyze("Kitchen project ideas", body)
    caps = issue(r, "body_caps")
    assert caps and len(caps["spans"]) >= 3
    assert r["stats"]["caps_ratio"] > 0.2
    _, fixed = apply_fix("", body, caps["fix"])
    assert "AMAZING" not in fixed and "Amazing" in fixed


def test_link_rules():
    links = " ".join(f"https://example.com/p{i}" for i in range(6))
    r = analyze("Project links", f"See {links} and http://bit.ly/abc and http://192.168.0.1/x")
    assert issue(r, "many_links") and issue(r, "shortener") and issue(r, "ip_link")
    assert r["stats"]["links"] == 8


def test_image_to_text_ratio():
    body = '<p>See our work.</p><img src="a.png"><img src="b.png">'
    r = analyze("Our recent projects", body, "html")
    assert issue(r, "image_heavy")
    assert r["stats"]["images"] == 2


def test_html_without_plain_text_is_flagged():
    r = analyze("Project update", "<p>" + CLEAN_BODY + "</p>", "html")
    assert issue(r, "no_plain_text")
    assert not issue(analyze("Project update", CLEAN_BODY), "no_plain_text")


def test_missing_unsubscribe_and_address():
    body = "Hi Sam,\n\nWe'd love to show you our latest bathroom remodel project.\n\n"
    r = analyze("Bathroom remodel", body)
    unsub = issue(r, "no_unsubscribe")
    assert unsub and issue(r, "no_address")
    _, fixed = apply_fix("", body, unsub["fix"])
    assert fixed.endswith("take you off our list.")
    assert not issue(analyze("Bathroom remodel", fixed), "no_unsubscribe")


def test_unsubscribe_fix_goes_inside_html_body():
    body = "<html><body><p>Hello, we design kitchens and bathrooms for homeowners in Texas.</p></body></html>"
    r = analyze("Kitchen design", body, "html")
    _, fixed = apply_fix("", body, issue(r, "no_unsubscribe")["fix"])
    assert fixed.endswith("</p></body></html>") and "unsubscribe" in fixed


def test_subject_length_rules():
    assert issue(analyze("Hi", CLEAN_BODY), "subject_short")
    assert issue(analyze("A" * 10 + " very long subject line " * 4, CLEAN_BODY), "subject_long")


def test_fake_reply_fix():
    subject = "RE: your kitchen"
    r = analyze(subject, CLEAN_BODY)
    new_subject, _ = apply_fix(subject, CLEAN_BODY, issue(r, "fake_reply")["fix"])
    assert new_subject == "your kitchen"


def test_keyword_fix_replaces_every_occurrence():
    body = "Click here to see the plans. Then click here again."
    r = analyze("Your plans", body)
    hit = next((k for k in r["keywords"] if k["matched"].lower() == "click here"), None)
    assert hit and hit["count"] == 2
    _, fixed = apply_fix("", body, hit["fix"])
    assert "click here" not in fixed.lower()


def test_instruction_alternatives_are_not_pasted_in():
    body = "Please click here to view our work."
    r = analyze("Our work", body)
    hit = next(k for k in r["keywords"] if k["matched"].lower() == "click here")
    _, fixed = apply_fix("", body, hit["fix"])
    assert "e.g." not in fixed and "Descriptive" not in fixed
    for k in r["keywords"]:
        if k["alternative"].startswith("Name the "):
            assert k["fix"] is None


def test_empty_input_is_safe():
    r = analyze("", "")
    assert r["score"] == 0 and r["inbox_score"] == 100
