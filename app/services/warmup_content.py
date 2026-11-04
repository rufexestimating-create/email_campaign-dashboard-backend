"""What warm-up mailboxes write to each other: short, ordinary work emails and replies.

Mailbox providers judge a new mailbox by whether its mail looks like real correspondence that people open and answer.
Each email is put together from several pools at random, so no two read the same and none look like a template.
"""
import random

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


def first_name(display_name: str | None, email: str) -> str:
    """'Ali Khan' -> 'Ali'; with no name, the address before the @: 'sara.ahmed@…' -> 'Sara'."""
    if display_name and display_name.strip():
        return display_name.strip().split()[0]
    local = email.split("@")[0]
    return (local.replace(".", " ").replace("_", " ").replace("-", " ").split() or [local])[0].capitalize()


def compose_initial(rng: random.Random, sender: str, recipient: str) -> tuple[str, str]:
    """A new email from `sender` to `recipient` (their first names): (subject, body)."""
    subject = rng.choice(SUBJECTS)
    paragraphs = rng.sample(BODIES, rng.choice([1, 1, 2]))
    question = rng.choice(QUESTIONS)
    body = "\n\n".join([rng.choice(GREETINGS).format(name=recipient), *paragraphs] + ([question] if question else []))
    return subject, f"{body}\n\n{rng.choice(SIGNOFFS)}\n{sender}"


def compose_reply(rng: random.Random, subject: str, sender: str, recipient: str) -> tuple[str, str]:
    """A short answer to `subject`: (subject with 'Re:', body)."""
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    greeting = rng.choice(GREETINGS + ["", ""])
    lines = ([greeting.format(name=recipient)] if greeting else []) + [rng.choice(REPLIES)]
    return reply_subject, "\n\n".join(lines) + f"\n\n{rng.choice(SIGNOFFS)}\n{sender}"
