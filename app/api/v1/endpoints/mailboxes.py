from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
from app.schemas.mailbox import ContentPreview, MailboxCreate, MailboxUpdate, NewsletterAdd
from app.services import warmup_content
from app.services import mailbox_warmup, newsletters
from app.services.mailbox_warmup import MailboxError

router = APIRouter(dependencies=[Depends(get_current_user)])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("", summary="Warm-up mailboxes with today's progress and where their emails landed")
def overview(user: dict = Depends(get_current_user)):
    return mailbox_warmup.overview(user["id"])


@router.get("/newsletters", summary="Newsletters to subscribe the mailboxes to, and the subscriptions so far")
def newsletter_list(user: dict = Depends(get_current_user)):
    return {"catalog": newsletters.catalog(), "subscriptions": newsletters.list_subscriptions(user["id"])}


@router.post("/{mailbox_id}/newsletters", status_code=status.HTTP_201_CREATED, summary="Record that a mailbox was subscribed to a newsletter")
def newsletter_add(mailbox_id: str, data: NewsletterAdd, user: dict = Depends(get_current_user)):
    if not mailbox_warmup.get_mailbox(user["id"], mailbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    try:
        return newsletters.add_subscription(user["id"], mailbox_id, data.model_dump())
    except ValueError as e:
        raise _bad(e)


@router.get("/newsletters/feed", summary="The newest newsletter emails in all the mailboxes' inboxes (live, newest first)")
def newsletter_feed(fresh: bool = False, user: dict = Depends(get_current_user)):
    if fresh:  # "Check now": look for new newsletters first, instead of waiting for the next 5-minute check
        newsletters.check_now(user["id"])
    return newsletters.feed(user["id"], fresh=fresh)


@router.get("/newsletters/{sub_id}/emails", summary="A newsletter's emails in the mailbox's inbox (last 30 days)")
def newsletter_emails(sub_id: str, user: dict = Depends(get_current_user)):
    try:
        return newsletters.list_emails(user["id"], sub_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except (MailboxError, ValueError) as e:
        raise _bad(e)


@router.get("/newsletters/{sub_id}/emails/{uid}", summary="One newsletter email, to read in the dashboard")
def newsletter_email(sub_id: str, uid: str, user: dict = Depends(get_current_user)):
    try:
        return newsletters.read_email(user["id"], sub_id, uid)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except (MailboxError, ValueError) as e:
        raise _bad(e)


@router.delete("/newsletters/{sub_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Stop handling a newsletter for a mailbox")
def newsletter_remove(sub_id: str, user: dict = Depends(get_current_user)):
    if not newsletters.remove_subscription(user["id"], sub_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/content-topics", summary="Topics the warm-up emails can be about")
def content_topics(user: dict = Depends(get_current_user)):
    return warmup_content.topics()


@router.post("/content-preview", summary="Sample warm-up emails for a topic and own subjects / messages (not saved)")
def content_preview(data: ContentPreview, user: dict = Depends(get_current_user)):
    content = {"topic": data.topic, "custom_subjects": data.custom_subjects, "custom_messages": data.custom_messages}
    return mailbox_warmup.preview(user["id"], content, data.mailbox_id)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a mailbox (its login is checked first)")
def add(data: MailboxCreate, user: dict = Depends(get_current_user)):
    try:
        return mailbox_warmup.add_mailbox(user["id"], data.model_dump())
    except (MailboxError, ValueError) as e:
        raise _bad(e)


@router.patch("/{mailbox_id}", summary="Rename, change the daily limit, pause / resume, or update the password")
def update(mailbox_id: str, data: MailboxUpdate, user: dict = Depends(get_current_user)):
    try:
        row = mailbox_warmup.update_mailbox(user["id"], mailbox_id, data.model_dump(exclude_unset=True))
    except (MailboxError, ValueError) as e:
        raise _bad(e)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    return row


@router.post("/{mailbox_id}/test", summary="Check the mailbox can still log in to send and read")
def test(mailbox_id: str, user: dict = Depends(get_current_user)):
    mb = mailbox_warmup.get_mailbox(user["id"], mailbox_id, with_password=True)
    if not mb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    try:
        return {"ok": True, **mailbox_warmup.test_connection(mb, mailbox_warmup.decrypt(mb["password_enc"]))}
    except (MailboxError, ValueError) as e:
        raise _bad(e)


@router.get("/{mailbox_id}/domain-check", summary="SPF, DKIM and DMARC records of the mailbox's domain")
def domain_check(mailbox_id: str, user: dict = Depends(get_current_user)):
    mb = mailbox_warmup.get_mailbox(user["id"], mailbox_id)
    if not mb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    try:
        return mailbox_warmup.domain_check(mb["email"])
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't look up the domain's DNS records right now. Try again in a minute.")


@router.post("/{mailbox_id}/send-test", summary="Send one warm-up email now (any time of day) and check where it lands")
def send_test(mailbox_id: str, background: BackgroundTasks, user: dict = Depends(get_current_user)):
    try:
        out = mailbox_warmup.send_test(user["id"], mailbox_id)
    except ValueError as e:
        raise _bad(e)
    background.add_task(mailbox_warmup.check_test, user["id"], out["id"])
    return out


@router.post("/{mailbox_id}/activity/{message_id}/send-now", summary="Send a planned warm-up email or reply now")
def send_planned_now(mailbox_id: str, message_id: str, background: BackgroundTasks, user: dict = Depends(get_current_user)):
    try:
        out = mailbox_warmup.send_planned_now(user["id"], mailbox_id, message_id)
    except ValueError as e:
        raise _bad(e)
    background.add_task(mailbox_warmup.check_test, user["id"], out["id"])
    return out


@router.get("/{mailbox_id}/activity", summary="The mailbox's latest warm-up emails, sent and received")
def activity(mailbox_id: str, user: dict = Depends(get_current_user)):
    if not mailbox_warmup.get_mailbox(user["id"], mailbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    return mailbox_warmup.activity(user["id"], mailbox_id, limit=100)  # the dashboard pages through them


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a mailbox and its warm-up history")
def delete(mailbox_id: str, user: dict = Depends(get_current_user)):
    if not mailbox_warmup.delete_mailbox(user["id"], mailbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
