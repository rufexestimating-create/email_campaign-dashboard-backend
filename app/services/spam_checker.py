"""Rule-based spam risk scoring for a subject + body (0 = clean, 100 = very likely filtered).

Every result also carries character positions (`spans`) into the subject/body exactly as the user typed them,
so the editor can highlight words, and `fix` edits the editor can apply in one click.
"""
import re
from functools import lru_cache
from html import unescape

from app.db.supabase import get_supabase
from app.services.spam_knowledge import builtin_keywords

KEYWORD_POINTS = {"high": 14, "medium": 7, "low": 3}
SUBJECT_MULTIPLIER = 1.5
PLACEHOLDER_RE = re.compile(r"{{\s*[a-zA-Z0-9_]+\s*}}")
URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)
SHORTENERS = re.compile(r"https?://(www\.)?(bit\.ly|tinyurl\.com|goo\.gl|t\.co|ow\.ly|is\.gd|buff\.ly|rebrand\.ly|cutt\.ly)/", re.I)
IP_LINK = re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}", re.I)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
SPACED_OUT = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")
TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>|&#?\w+;")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
MONEY_RE = re.compile(r"[$€£]\s?[\d,.]*")
MULTI_EXCLAIM = re.compile(r"!{2,}")
FAKE_REPLY = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.I)
UNSUBSCRIBE_RE = re.compile(r"unsubscribe|opt[\s-]?out|stop (?:receiving|getting) (?:these )?emails|manage (?:your )?(?:email )?preferences", re.I)
# CAN-SPAM needs a physical postal address: a street line, a "ST 12345" state + ZIP, or a PO box
STREET_RE = re.compile(r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,4}(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|ct|court|pl|place|pkwy|parkway|hwy|highway|suite|ste)\b", re.I)
STATE_ZIP_RE = re.compile(r"\b[A-Z]{2},?\s+\d{5}(?:-\d{4})?\b")
PO_BOX_RE = re.compile(r"\bP\.?\s?O\.?\s+Box\s+\d+", re.I)
UNSUBSCRIBE_LINE = "Don't want these emails? Reply \"unsubscribe\" and we'll take you off our list."
WORDS_PER_IMAGE = 50
REASON_LIMIT = 5
# Acronyms that are normal in architecture / business writing and shouldn't count as "shouting"
ALLOWED_CAPS = {
    "BIM", "CAD", "LEED", "BREEAM", "RIBA", "AIA", "HVAC", "MEP", "CEO", "COO", "CFO", "GM", "USA", "UAE", "PDF", "FAQ",
    "ROI", "ESG", "NDA", "RFP", "RFQ", "ASAP", "DWG", "IFC", "GIS", "UK", "EU", "LLC", "LTD", "KSA", "CPD", "ISO", "NEC",
}


def _phrase_regex(phrase: str) -> re.Pattern:
    if phrase == "% off":
        return re.compile(r"\d+\s*%\s*off\b", re.I)
    parts = [re.escape(p) for p in phrase.split()]
    # Word boundaries only next to letters/digits, so "#1" and "earn $" still match; "earn $" also takes the amount
    start = r"(?<![A-Za-z0-9])" if phrase[0].isalnum() else ""
    end = r"\s?[\d,.]*k?(?![A-Za-z0-9])" if phrase[-1] in "$€£" else r"(?![A-Za-z0-9])" if phrase[-1].isalnum() else ""
    return re.compile(start + r"\s+".join(parts) + end, re.I)


@lru_cache(maxsize=1)
def _builtin_compiled() -> list[tuple[dict, re.Pattern]]:
    entries = sorted(builtin_keywords(), key=lambda k: len(k["phrase"]), reverse=True)
    return [(k, _phrase_regex(k["phrase"])) for k in entries]


def custom_keywords(user_id: str | None) -> list[dict]:
    if not user_id:
        return []
    try:
        rows = get_supabase().table("spam_keywords").select("id, phrase, severity, alternative, note").eq("user_id", user_id).execute().data
    except Exception:
        return []  # table not created yet: built-in list still works
    return [
        {
            "id": r["id"], "phrase": r["phrase"], "severity": r["severity"], "alternative": r.get("alternative") or "(rephrase)",
            "category": "custom", "category_name": "Your keywords", "why": r.get("note") or "Added by your team.", "source": "custom",
        }
        for r in rows
    ]


def to_plain_text(body: str, content_type: str) -> str:
    if content_type != "html":
        return body
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text)
    return unescape(re.sub(r"<[^>]+>", " ", text))


def _mask(raw: str, content_type: str) -> str:
    """Same length as `raw`, with {{placeholders}} (and HTML tags/entities) blanked out.
    Matching on this keeps every position pointing into the text the user actually typed."""
    blank = lambda m: re.sub(r"\S", " ", m.group(0))
    out = PLACEHOLDER_RE.sub(blank, raw)
    return TAG_RE.sub(blank, out) if content_type == "html" else out


def _span(field: str, m: re.Match) -> dict:
    return {"field": field, "start": m.start(), "end": m.end()}


def _edit(field: str, start: int, end: int, text: str) -> dict:
    return {"field": field, "start": start, "end": end, "text": text}


def _fix(label: str, edits: list[dict]) -> dict:
    # Applied last-to-first, so earlier positions stay valid while the editor applies them
    return {"label": label, "edits": sorted(edits, key=lambda e: (e["field"], -e["start"]))}


def _replacement(alternative: str, matched: str) -> str | None:
    """Same rule as the editor: "(remove it)" deletes the phrase, "fee proposal (or …)" keeps only the words.
    None when the alternative is advice to write something specific ("Name the award…"), which can't be auto-applied."""
    if alternative.startswith("("):
        return ""
    example = re.search(r'e\.g\. "([^"]+)"', alternative)
    if example:
        text = example.group(1)
    elif alternative.startswith("Name the "):
        return None
    else:
        text = re.sub(r"\s*\(.*\)$", "", alternative)
    return text[:1].upper() + text[1:] if matched[:1].isupper() and text else text


def _sentence_case(subject: str) -> str:
    """'BIG NEWS FOR {{company}}' -> 'Big news for {{company}}'; placeholders and known acronyms are kept as they are."""
    parts = re.split(f"({PLACEHOLDER_RE.pattern})", subject)
    out = "".join(p if PLACEHOLDER_RE.fullmatch(p) else WORD_RE.sub(lambda m: m.group(0) if m.group(0) in ALLOWED_CAPS else m.group(0).lower(), p) for p in parts)
    first = re.search(r"[A-Za-z{]", out)
    if first and out[first.start()] != "{":
        out = out[:first.start()] + out[first.start()].upper() + out[first.start() + 1:]
    return out


def _issue(issues: list, id_: str, severity: str, location: str, title: str, suggestion: str, points: float,
           spans: list[dict] | None = None, fix: dict | None = None):
    issues.append({
        "id": id_, "severity": severity, "location": location, "title": title, "suggestion": suggestion, "points": round(points),
        "spans": spans or [], "fix": fix,
    })


def _find_keywords(masked: str, raw: str, location: str, compiled: list[tuple[dict, re.Pattern]]) -> list[dict]:
    """Longest phrases first; a span already matched by a longer phrase isn't counted again ("free consultation" vs "free")."""
    taken: list[tuple[int, int]] = []
    hits = []
    for kw, rx in compiled:
        matches = [m for m in rx.finditer(masked) if not any(m.start() < e and m.end() > s for s, e in taken)]
        if not matches:
            continue
        taken.extend((m.start(), m.end()) for m in matches)
        base = KEYWORD_POINTS[kw["severity"]] * (SUBJECT_MULTIPLIER if location == "subject" else 1)
        points = base + base * 0.5 * min(len(matches) - 1, 2)
        matched = raw[matches[0].start():matches[0].end()]
        fix = None
        if _replacement(kw["alternative"], matched) is not None:
            edits = []
            for m in matches:
                # Tags inside the phrase ("free</b> estimate") are kept so the HTML stays balanced
                new = _replacement(kw["alternative"], raw[m.start():m.end()]) + "".join(re.findall(r"<[^>]+>", raw[m.start():m.end()]))
                # Removing a phrase also takes one following space, so no double gap is left behind
                end = m.end() + 1 if not new and raw[m.end():m.end() + 1] == " " else m.end()
                edits.append(_edit(location, m.start(), end, new))
            fix = _fix("Remove" if kw["alternative"].startswith("(") else f"Replace with \"{_replacement(kw['alternative'], matched)}\"", edits)
        hits.append({
            **kw, "matched": matched, "location": location, "count": len(matches), "points": round(points),
            "spans": [_span(location, m) for m in matches], "fix": fix,
        })
    return hits


def _reasons(keywords: list[dict], issues: list[dict]) -> list[dict]:
    """The few things that cost the most points, worded for a reasons list."""
    items = [
        {
            "ref": f"kw:{k['phrase']}", "severity": k["severity"], "location": k["location"], "points": k["points"],
            "title": f"Spam trigger phrase \"{k['matched']}\"" + (f" ({k['count']}×)" if k["count"] > 1 else ""),
            "suggestion": "Remove it." if k["alternative"].startswith("(")
            else f"Instead: {k['alternative']}." if '"' in k["alternative"] else f"Use \"{k['alternative']}\" instead.",
        }
        for k in keywords
    ] + [
        {"ref": i["id"], "severity": i["severity"], "location": i["location"], "points": i["points"], "title": i["title"], "suggestion": i["suggestion"]}
        for i in issues
    ]
    return sorted(items, key=lambda r: -r["points"])[:REASON_LIMIT]


def analyze(subject: str, body: str, content_type: str = "text", user_id: str | None = None) -> dict:
    subject_raw = subject or ""
    body_raw = body or ""
    subj = PLACEHOLDER_RE.sub("Alex", subject_raw).strip()
    text = PLACEHOLDER_RE.sub("Alex", to_plain_text(body_raw, content_type))
    subj_mask = _mask(subject_raw, "text")
    body_mask = _mask(body_raw, content_type)
    is_html = content_type == "html"
    issues: list[dict] = []

    compiled = list(_builtin_compiled())
    custom = custom_keywords(user_id)
    if custom:
        compiled = sorted(compiled + [(k, _phrase_regex(k["phrase"])) for k in custom], key=lambda x: len(x[0]["phrase"]), reverse=True)
    keywords = _find_keywords(subj_mask, subject_raw, "subject", compiled) + _find_keywords(body_mask, body_raw, "body", compiled)

    # --- Subject line checks ---
    letters = [c for c in subj if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        _issue(issues, "subject_caps", "high", "subject", "Subject line is mostly CAPITAL letters", "Write it in normal sentence case.", 18,
               [{"field": "subject", "start": 0, "end": len(subject_raw)}],
               _fix("Change to sentence case", [_edit("subject", 0, len(subject_raw), _sentence_case(subject_raw))]))
    bangs = [_span("subject", m) for m in re.finditer("!", subj_mask)]
    remove_bangs = _fix("Remove the exclamation marks", [_edit("subject", s["start"], s["end"], "") for s in bangs])
    if len(bangs) >= 2:
        _issue(issues, "subject_exclaim", "medium", "subject", "Several exclamation marks in the subject", "Use at most one, or none.", 9, bangs, remove_bangs)
    elif len(bangs) == 1:
        _issue(issues, "subject_exclaim_one", "low", "subject", "Exclamation mark in the subject", "Business readers respond better to a calm subject line.", 2, bangs, remove_bangs)
    fake = FAKE_REPLY.match(subject_raw)
    if fake:
        _issue(issues, "fake_reply", "high", "subject", "Subject starts with RE: or FW:", "Don't pretend to be a reply. Filters and readers see this as deceptive.", 16,
               [_span("subject", fake)], _fix("Remove the prefix", [_edit("subject", fake.start(), fake.end(), "")]))
    if subj and len(subj) < 15:
        _issue(issues, "subject_short", "low", "subject", "Subject line is very short", "Aim for 25–60 characters that say what the email is about.", 3)
    elif len(subj) > 70:
        _issue(issues, "subject_long", "low", "subject", "Subject line is long and may be cut off", "Keep it under 60 characters.", 3,
               [{"field": "subject", "start": 60, "end": len(subject_raw)}])
    emojis = [_span("subject", m) for m in EMOJI_RE.finditer(subj_mask)]
    if len(emojis) > 1:
        _issue(issues, "subject_emoji", "low", "subject", "Several emojis in the subject", "Use one at most for professional contacts.", 4, emojis)
    money_subj = [_span("subject", m) for m in re.finditer(r"[$€£]\s?\d[\d,.]*k?", subj_mask)]
    if money_subj:
        _issue(issues, "subject_money", "medium", "subject", "Price or money amount in the subject", "Mention pricing inside the email instead.", 7, money_subj)

    # --- Body checks ---
    words = WORD_RE.findall(text)
    caps = [m for m in WORD_RE.finditer(body_mask) if len(m.group(0)) >= 4 and m.group(0).isupper() and m.group(0) not in ALLOWED_CAPS]
    if len(caps) >= 3:
        names = ", ".join(list(dict.fromkeys(m.group(0) for m in caps))[:3])
        _issue(issues, "body_caps", "medium", "body", f"{len(caps)} words in ALL CAPS (e.g. {names})", "Use bold sparingly instead of capital letters.",
               min(4 + len(caps) * 1.5, 12), [_span("body", m) for m in caps],
               _fix("Change to normal case", [_edit("body", m.start(), m.end(), m.group(0).capitalize()) for m in caps]))
    multi = list(MULTI_EXCLAIM.finditer(body_mask))
    exclaims = body_mask.count("!")
    if multi:
        _issue(issues, "body_multi_exclaim", "medium", "body", "Repeated exclamation marks (!!)", "Use a single full stop or one exclamation mark.", 7,
               [_span("body", m) for m in multi], _fix("Use a single \"!\"", [_edit("body", m.start(), m.end(), "!") for m in multi]))
    elif exclaims > 3:
        _issue(issues, "body_exclaims", "low", "body", f"{exclaims} exclamation marks in the message", "Keep it to one or two.", 4,
               [_span("body", m) for m in re.finditer("!", body_mask)])
    money = [m for m in MONEY_RE.finditer(body_mask)]
    if len(money) >= 3:
        _issue(issues, "money_signs", "medium", "body", f"{len(money)} money signs ($) in the message", "Mention price once, or invite them to ask for a quote.", 6,
               [_span("body", m) for m in money])
    spaced = [_span("subject", m) for m in SPACED_OUT.finditer(subj_mask)] + [_span("body", m) for m in SPACED_OUT.finditer(body_mask)]
    if spaced:
        raw_of = {"subject": subject_raw, "body": body_raw}
        _issue(issues, "spaced_out", "medium", spaced[0]["field"], "Spaced-out letters (like F R E E)", "Filters recognise this trick. Write words normally.", 9, spaced,
               _fix("Join the letters", [_edit(s["field"], s["start"], s["end"], raw_of[s["field"]][s["start"]:s["end"]].replace(" ", "")) for s in spaced]))

    links = list(URL_RE.finditer(body_raw))
    shortened = [_span("body", m) for m in SHORTENERS.finditer(body_raw)]
    if shortened:
        _issue(issues, "shortener", "high", "body", "Link shortener used (bit.ly, tinyurl…)", "Link to your own website address in full.", 15, shortened)
    ip_links = [_span("body", m) for m in IP_LINK.finditer(body_raw)]
    if ip_links:
        _issue(issues, "ip_link", "high", "body", "Link points to a raw IP address", "Use your domain name in links.", 15, ip_links)
    if len(links) > 5:
        _issue(issues, "many_links", "medium", "body", f"{len(links)} links in the email", "Keep it to 2–3 relevant links.", 8, [_span("body", m) for m in links])

    images = list(re.finditer(r"(?i)<img\b[^>]*>", body_raw)) if is_html else []
    if is_html:
        if images and len(words) / len(images) < WORDS_PER_IMAGE:
            _issue(issues, "image_heavy", "medium", "body", f"{len(images)} image{'s' if len(images) != 1 else ''} for only {len(words)} words of text",
                   f"Add real text: about {WORDS_PER_IMAGE} words per image. Image-heavy emails are often filtered.", 10, [_span("body", m) for m in images])
        unsafe = [_span("body", m) for m in re.finditer(r"(?i)<(form|script|iframe)\b", body_raw)]
        if unsafe:
            _issue(issues, "html_unsafe", "high", "body", "Contains forms, scripts or embedded frames", "Email clients block these, and filters treat them as dangerous.", 15, unsafe)
        if text.strip():
            _issue(issues, "no_plain_text", "low", "body", "HTML email without a plain-text version",
                   "Filters trust emails that also include a plain-text copy. Write this one as plain text if it doesn't need HTML.", 4)
    if text.strip() and len(words) < 20:
        _issue(issues, "body_short", "low", "body", "Message is very short", "A few sentences of context helps filters and readers trust the email.", 4)
    elif len(words) > 600:
        _issue(issues, "body_long", "low", "body", "Message is very long", "Keep business emails under about 300 words and link to details.", 3)

    # --- Compliance (CAN-SPAM): a way to opt out and a postal address ---
    has_unsubscribe = bool(UNSUBSCRIBE_RE.search(body_raw))
    has_address = bool(STREET_RE.search(text) or STATE_ZIP_RE.search(text) or PO_BOX_RE.search(text))
    if text.strip() and not has_unsubscribe:
        if is_html:  # inside </body> when the template is a full HTML document
            close = body_raw.lower().rfind("</body>")
            at = close if close >= 0 else len(body_raw)
            edit = _edit("body", at, at, f"<p>{UNSUBSCRIBE_LINE}</p>")
        else:  # replaces trailing blank lines so the line sits one blank line below the text
            edit = _edit("body", len(body_raw.rstrip()), len(body_raw), f"\n\n{UNSUBSCRIBE_LINE}")
        _issue(issues, "no_unsubscribe", "medium", "body", "No way to unsubscribe", "US law (CAN-SPAM) requires a clear way to opt out, and it keeps people from pressing \"Report spam\".", 10,
               fix=_fix("Add an unsubscribe line", [edit]))
    if text.strip() and not has_address:
        _issue(issues, "no_address", "low", "body", "No postal address", "Add your company's street address or PO box at the bottom. CAN-SPAM requires it.", 5)

    total = sum(k["points"] for k in keywords) + sum(i["points"] for i in issues)
    score = min(100, round(total))
    level = "good" if score < 20 else "fair" if score < 45 else "poor"
    label = {"good": "Looks good", "fair": "Needs a few changes", "poor": "Likely to land in spam"}[level]
    keywords = sorted(keywords, key=lambda k: -k["points"])
    issues = sorted(issues, key=lambda i: -i["points"])

    personalised = bool(PLACEHOLDER_RE.search(subject_raw) or PLACEHOLDER_RE.search(body_raw))
    letters_body = [c for c in text if c.isalpha()]
    return {
        "score": score,
        "inbox_score": 100 - score,
        "level": level,
        "risk": {"good": "low", "fair": "medium", "poor": "high"}[level],
        "label": label,
        "estimate_note": "Rules-based estimate of spam risk. No tool can guarantee inbox placement.",
        "reasons": _reasons(keywords, issues),
        "highlights": sorted(
            [{**s, "severity": k["severity"], "ref": f"kw:{k['phrase']}"} for k in keywords for s in k["spans"]]
            + [{**s, "severity": i["severity"], "ref": i["id"]} for i in issues for s in i["spans"]],
            key=lambda h: (h["field"], h["start"]),
        ),
        "keywords": keywords,
        "issues": issues,
        "stats": {
            "subject_length": len(subj),
            "word_count": len(words),
            "links": len(links),
            "images": len(images),
            "exclamations": subj_mask.count("!") + exclaims,
            "money_signs": len(money),
            "caps_ratio": round(sum(c.isupper() for c in letters_body) / len(letters_body), 2) if letters_body else 0,
            "has_unsubscribe": has_unsubscribe,
            "has_address": has_address,
            "personalised": personalised,
        },
    }
