"""What warm-up mailboxes write to each other: short, ordinary work emails and replies.

Mailbox providers judge a new mailbox by whether its mail looks like real correspondence that people open and answer.
Each email is put together from several pools at random, so no two read the same and none look like a template.
"""
import random
import re

SUBJECTS = [
    "Quick question about Thursday",
    "Notes from today",
    "Drawings for the Miller house",
    "Can we move our call?",
    "Revised estimate",
    "Site visit next week",
    "Following up on the floor plan",
    "Lunch on Friday?",
    "Updated schedule",
    "Client feedback",
    "Materials list",
    "Invoice question",
    "Meeting agenda",
    "Photos from the site",
    "A small change to the layout",
    "Checking in",
    "Plans for next week",
    "Kitchen measurements",
    "Thanks for today",
    "Permit status",
    "Window sizes",
    "Re-check the roof pitch?",
    "Draft proposal",
    "Office hours over the holiday",
    "Supplier quote",
    "Monday catch-up",
    "Question about the contract",
    "Leave next month",
    "New printer in the office",
    "Timesheet for this week",
    "Bathroom tile samples",
    "Parking at the site",
    "Training session on Wednesday",
    "Shared folder access",
    "Delivery date for the cabinets",
    "Landscape plan",
    "Lighting options",
    "Reminder: team meeting",
    "Budget for the Johnson project",
    "Feedback on the 3D render",
]

GREETINGS = ["Hi {name},", "Hello {name},", "Hey {name},", "Hi {name},", "Good morning {name},", "{name},"]

BODIES = [
    "I went through the drawings you sent this morning. Everything looks fine apart from the stair width on the first floor. Could you take another look when you have a minute?",
    "Just a reminder that we're meeting the client on Thursday at 11. I'll bring the printed plans if you can bring the material samples.",
    "Can we push our call to tomorrow afternoon? Something came up on site and I won't be back at my desk until late.",
    "Just a heads-up that the revised estimate is nearly done. I should have it to you by the end of the day.",
    "The client liked the second option for the kitchen layout. They asked whether the island could be a little smaller so the walkway is wider.",
    "Do you know if the permit for the Oak Street project has come through yet? The builder wants to start the week after next.",
    "Thanks again for your help with the measurements yesterday. It saved me a second trip out there.",
    "I spoke to the supplier and they can deliver the timber on Monday. Let me know if that works for the site team.",
    "Are you free for a quick chat sometime this week? I'd like to go over the timeline for the next two projects.",
    "The roof pitch on sheet 4 doesn't quite match the elevation drawing. Could you check which one is right?",
    "I've updated the materials list with the new tile prices. The total is a little higher than we expected, mostly because of the flooring.",
    "Good news, the client approved the floor plan. We can move on to the electrical layout now.",
    "I'll be out of the office on Friday, so if anything urgent comes in please give me a call instead of emailing.",
    "Could you send me the final version of the site photos? I want to add a few of them to the report.",
    "We had a few questions from the client about the window sizes in the living room. I said we'd get back to them by Wednesday.",
    "I've put together a rough agenda for Monday's meeting. Let me know if there's anything you'd like to add.",
    "The supplier's quote came in lower than last time, which is a nice surprise. I think we should go ahead with them.",
    "Is lunch on Friday still on? I was thinking of the place near the office around 1.",
    "I checked the invoice and I think the hours for the second week were counted twice. Could you have a look before we send it?",
    "The builder asked whether we could add a small storage room under the stairs. I think there's just enough space.",
    "I've added you to the shared folder for the new project. Let me know if you can't open anything in there.",
    "Could you fill in your timesheet by Friday? Accounts want to close the month early this time.",
    "The cabinet supplier says delivery will slip by about a week. I've told the client, and they're fine with it.",
    "I'm planning to take a few days off at the end of next month. Would you be able to cover the Tuesday meetings?",
    "The new render looks great. The only thing I'd change is the colour of the front door, it feels a bit dark.",
    "There's a short training session on the new drawing software on Wednesday at 3. It should take about an hour.",
    "The client wants to see two or three lighting options for the dining room before they decide.",
    "I had a look at the contract and there's one clause about payment dates I'd like to go over with you.",
    "Parking near the site is tight this week because of the road works, so it might be easier to come by train.",
    "I've booked the meeting room for Monday morning. Coffee will be there from half past nine.",
    "The landscape architect sent over the first draft of the garden plan. I've saved it in the project folder.",
    "We're still waiting on the tile samples for the bathroom. The supplier promised them by Thursday.",
    "Could you double-check the room sizes on the second floor? The client measured one of the bedrooms and got a different number.",
    "The budget for the Johnson project is a little tight, so we may need to suggest a cheaper flooring option.",
    "I've moved our weekly catch-up to 10 on Monday, since a few people are out on Friday.",
]

QUESTIONS = [
    "What do you think?",
    "Does that work for you?",
    "Let me know.",
    "Any thoughts?",
    "Let me know if you need anything from me.",
    "Can you get back to me when you have a moment?",
    "",
    "",
]

SIGNOFFS = ["Thanks,", "Best,", "Cheers,", "Thanks a lot,", "Regards,", "Talk soon,", "Best regards,"]

REPLIES = [
    "Thanks, that works for me.",
    "Sounds good. I'll take a look this afternoon and get back to you.",
    "Got it, thanks for letting me know.",
    "Sure, no problem. Tomorrow afternoon is fine.",
    "Good catch. I'll check it and send you the corrected version.",
    "Great news, thanks!",
    "Yes, Friday works. See you then.",
    "I'll have it ready by tomorrow morning.",
    "Thanks for the update. Let's talk about it on Monday.",
    "That's fine with me. Go ahead.",
    "I think you're right. Let's go with the second option.",
    "Thanks, I'll send them over in a bit.",
    "Perfect, thank you.",
    "Noted. I'll let the client know.",
    "Makes sense. I'll update the plans today.",
    "No problem, I can cover those.",
    "Thanks, I'll look at it before the meeting.",
    "Good to know. I'll plan around it.",
    "Will do. I'll send it over by Friday.",
    "Thanks for sorting that out.",
    "Sounds like a plan. Let's catch up on Monday.",
    "Got it. I'll let the team know.",
    "Agreed, let's keep it simple.",
]


# ---------- Topics: what the warm-up emails are about ----------
# The pools above are the "architecture" topic. Other businesses pick a topic that fits their own work, so their warm-up
# emails read like their real mail. Users can also add their own subjects and messages (custom_subjects / custom_messages).

TOPICS: dict[str, dict] = {
    "architecture": {"label": "Architecture & design", "subjects": SUBJECTS, "bodies": BODIES},
    "general": {
        "label": "General business / office",
        "subjects": [
            "Quick question", "Notes from this morning", "Meeting on Thursday", "Can we move our call?", "Updated schedule",
            "Monday catch-up", "Draft for review", "Following up", "Plans for next week", "Thanks for today",
            "Reminder: team meeting", "Shared folder access", "Leave next month", "Office hours over the holiday", "Lunch on Friday?",
            "New process for expenses", "Agenda for the review", "Training session on Wednesday",
        ],
        "bodies": [
            "Just a reminder that we're meeting on Thursday at 11. I'll send the agenda the day before.",
            "Can we push our call to tomorrow afternoon? Something came up and I won't be at my desk until late.",
            "I've put the draft in the shared folder. Could you have a look before Friday?",
            "Thanks again for your help yesterday. It saved me a lot of time.",
            "Are you free for a quick chat sometime this week? I'd like to go over the plan for next month.",
            "I'll be out of the office on Friday, so please call me if anything urgent comes in.",
            "I've added you to the shared folder. Let me know if you can't open anything in there.",
            "Could you fill in your timesheet by Friday? Accounts want to close the month early this time.",
            "I've booked the meeting room for Monday morning. Coffee will be there from half past nine.",
            "I'm planning to take a few days off at the end of next month. Would you be able to cover for me?",
            "There's a short training session on Wednesday at 3. It should take about an hour.",
            "Is lunch on Friday still on? I was thinking of the place near the office around 1.",
            "I've moved our weekly catch-up to 10 on Monday, since a few people are out on Friday.",
            "The new expense form is live. From next week, receipts go through it instead of email.",
            "I went through the notes from this morning and added a few points at the end.",
        ],
    },
    "construction": {
        "label": "Construction & contracting",
        "subjects": [
            "Site visit next week", "Concrete delivery", "Inspection on Tuesday", "Updated site schedule", "Safety meeting",
            "Supplier quote", "Change order", "Crew for Monday", "Permit status", "Photos from the site", "Scaffolding",
            "Materials list", "Weather delay", "Final walkthrough", "Invoice for last week",
        ],
        "bodies": [
            "The concrete delivery is booked for Monday at 7. Can someone be on site to sign for it?",
            "The inspector moved the visit to Tuesday morning. We should have the framing finished by then.",
            "I've updated the site schedule with the new dates for the electrical work.",
            "Rain is forecast for Wednesday, so we might lose a day on the roof. I'll keep you posted.",
            "The supplier's quote for the steel came in a little lower than last time.",
            "The client asked for a change to the kitchen wall. I'll put together a change order today.",
            "We'll need two extra people on Monday for the drywall. Can you check who's free?",
            "Still waiting on the permit for the extension. The office said it should come through this week.",
            "I've uploaded the site photos to the project folder, including the ones of the foundation.",
            "Safety meeting is on Friday at 8 before work starts. It should only take twenty minutes.",
            "The scaffolding can come down on Thursday once the painters are done.",
            "The final walkthrough with the client is set for next Friday at 10.",
            "I checked last week's invoice and the hours for the plumbing look right.",
        ],
    },
    "real_estate": {
        "label": "Real estate",
        "subjects": [
            "Viewing on Saturday", "New listing photos", "Offer on Oak Street", "Price update", "Open house", "Tenant question",
            "Lease renewal", "Valuation report", "Documents for closing", "Follow-up from the viewing", "Market update",
            "Keys for the apartment", "Inspection report",
        ],
        "bodies": [
            "The buyers asked for a second viewing on Saturday morning. Does 10 work for the owners?",
            "The photographer sent the new listing photos. I've picked the best twelve for the website.",
            "We received an offer on the Oak Street house, a little under the asking price. Let's talk it through.",
            "The owners agreed to lower the price slightly. I'll update the listing this afternoon.",
            "The open house went well, about fifteen people came through and two asked for more details.",
            "One of the tenants asked whether they can paint the second bedroom. What do you think?",
            "The lease for the apartment on Main Street is up next month. Should I send the renewal?",
            "The valuation came back in line with what we expected.",
            "I've collected the documents for closing. We're only missing the signed disclosure form.",
            "The inspection report is in. There are a few small things, nothing serious.",
            "I'll drop the keys off at the office tomorrow morning.",
            "Prices in the area have held steady this quarter. I'll share a short summary on Monday.",
        ],
    },
    "marketing": {
        "label": "Marketing agency",
        "subjects": [
            "Campaign results", "Draft for the newsletter", "Social posts for next week", "Client feedback", "New brief",
            "Ad budget", "Content calendar", "Logo options", "Landing page copy", "Monthly report", "Photo shoot",
            "Website changes",
        ],
        "bodies": [
            "Last week's campaign did better than expected, especially on the second ad.",
            "I've drafted the newsletter for Thursday. Could you read through it before it goes out?",
            "The social posts for next week are in the shared folder, ready for approval.",
            "The client liked the new direction but wants the colours a bit brighter.",
            "We got a new brief from the bakery client. They want a small spring campaign.",
            "The ad budget for this month is almost used up. Should we pause the weaker ads?",
            "I've updated the content calendar with the product launch dates.",
            "The designer sent three logo options. I like the second one best.",
            "Could you look at the landing page copy? I think the headline could be shorter.",
            "The monthly report is nearly done. I'll send it over by the end of the day.",
            "The photo shoot is booked for Tuesday afternoon at the client's shop.",
        ],
    },
    "software": {
        "label": "Software / IT",
        "subjects": [
            "Release on Thursday", "Bug in the login page", "Code review", "Sprint planning", "Server maintenance",
            "New feature request", "Test results", "Access to the staging server", "Standup notes", "Documentation update",
            "Client demo", "Backup check",
        ],
        "bodies": [
            "The release is planned for Thursday afternoon. Everything is on track so far.",
            "A customer reported a bug on the login page. I've reproduced it and I'm looking into it now.",
            "Could you review my pull request when you have a minute? It's a small change.",
            "Sprint planning is on Monday at 10. Please bring your estimates for the open tickets.",
            "There's server maintenance on Saturday night, so the app may be down for about an hour.",
            "The client asked for an export button on the reports page. I've added it to the backlog.",
            "All tests passed on the last build except one, which I'm fixing now.",
            "I've given you access to the staging server. Let me know if the login works.",
            "I've updated the documentation for the new settings page.",
            "The client demo went well. They had a few questions about the dashboard.",
            "I checked the backups this morning and everything is running as it should.",
        ],
    },
    "finance": {
        "label": "Accounting / finance",
        "subjects": [
            "Invoices for this month", "Quarterly report", "Receipts", "Payroll", "Budget review", "Tax documents",
            "Payment reminder", "Expense claims", "Bank reconciliation", "Year-end checklist", "Client statement",
        ],
        "bodies": [
            "I've sent out this month's invoices. Two clients still need to confirm their addresses.",
            "The quarterly report is nearly finished. I'll send it over by Thursday.",
            "Could you send me the receipts from last week's trip? I need them for the expense claim.",
            "Payroll will go out on Friday as usual.",
            "Let's go through the budget on Monday. A couple of lines are higher than planned.",
            "I've collected most of the tax documents. We're only missing the bank statement for March.",
            "One client is a little late with their payment. I'll send a friendly reminder tomorrow.",
            "The bank reconciliation for last month is done and everything matches.",
            "I've started the year-end checklist and shared it in the folder.",
            "The client asked for an updated statement. I'll send it this afternoon.",
        ],
    },
}
CUSTOM_ONLY = "custom"  # topic: only the user's own subjects / messages
DEFAULT_TOPIC = "architecture"
OWN_SHARE = 0.6  # with own lines and a topic, this share of emails use the own lines


def topics() -> list[dict]:
    return [{"id": k, "label": v["label"]} for k, v in TOPICS.items()] + [{"id": CUSTOM_ONLY, "label": "Only my own subjects and messages"}]


def _pools(content: dict | None) -> tuple[list[str], list[str], list[str], list[str]]:
    """(topic subjects, topic bodies, own subjects, own messages) for a mailbox's content settings."""
    content = content or {}
    topic = content.get("topic") or DEFAULT_TOPIC
    own_subjects = [s.strip() for s in content.get("custom_subjects") or [] if s and s.strip()]
    own_messages = [m.strip() for m in content.get("custom_messages") or [] if m and m.strip()]
    if topic == CUSTOM_ONLY and (own_subjects or own_messages):
        pack = TOPICS["general"]  # fills whichever half has no own lines
        return ([] if own_subjects else pack["subjects"]), ([] if own_messages else pack["bodies"]), own_subjects, own_messages
    pack = TOPICS.get(topic, TOPICS[DEFAULT_TOPIC] if topic != CUSTOM_ONLY else TOPICS["general"])
    return pack["subjects"], pack["bodies"], own_subjects, own_messages


def _pick(rng: random.Random, topic_pool: list[str], own: list[str]) -> str:
    if own and (not topic_pool or rng.random() < OWN_SHARE):
        return rng.choice(own)
    return rng.choice(topic_pool)


def first_name(display_name: str | None, email: str) -> str:
    """'Ali Khan' -> 'Ali'; with no name, the address before the @: 'sara.ahmed@…' -> 'Sara'."""
    if display_name and display_name.strip():
        return display_name.strip().split()[0]
    local = email.split("@")[0]
    return (local.replace(".", " ").replace("_", " ").replace("-", " ").split() or [local])[0].capitalize()


GREETING_RE = re.compile(r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b", re.I)


def compose_initial(rng: random.Random, sender: str, recipient: str, content: dict | None = None) -> tuple[str, str]:
    """A new email from `sender` to `recipient` (their first names): (subject, body).

    `content` is the sending mailbox's choice: {topic, custom_subjects, custom_messages}. Each email is put together at
    random from the topic's pools and the user's own lines, with a greeting and sign-off, so no two read the same.
    In own messages, {first_name} becomes the recipient's first name."""
    subjects, bodies, own_subjects, own_messages = _pools(content)
    subject = _pick(rng, subjects, own_subjects)
    if own_messages and (not bodies or rng.random() < OWN_SHARE):
        own = rng.choice(own_messages).replace("{first_name}", recipient).replace("{name}", recipient)
        parts = [own] if GREETING_RE.match(own) else [rng.choice(GREETINGS).format(name=recipient), own]
        return subject, "\n\n".join(parts) + f"\n\n{rng.choice(SIGNOFFS)}\n{sender}"
    paragraphs = rng.sample(bodies, min(len(bodies), rng.choice([1, 1, 2])))
    question = rng.choice(QUESTIONS)
    body = "\n\n".join([rng.choice(GREETINGS).format(name=recipient), *paragraphs] + ([question] if question else []))
    return subject, f"{body}\n\n{rng.choice(SIGNOFFS)}\n{sender}"


def compose_reply(rng: random.Random, subject: str, sender: str, recipient: str) -> tuple[str, str]:
    """A short answer to `subject`: (subject with 'Re:', body)."""
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    greeting = rng.choice(GREETINGS + ["", ""])
    lines = ([greeting.format(name=recipient)] if greeting else []) + [rng.choice(REPLIES)]
    return reply_subject, "\n\n".join(lines) + f"\n\n{rng.choice(SIGNOFFS)}\n{sender}"
