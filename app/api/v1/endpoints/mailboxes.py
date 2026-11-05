from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
from app.schemas.mailbox import MailboxCreate, MailboxUpdate, NewsletterAdd
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


@router.get("/{mailbox_id}/activity", summary="The mailbox's latest warm-up emails, sent and received")
def activity(mailbox_id: str, user: dict = Depends(get_current_user)):
    if not mailbox_warmup.get_mailbox(user["id"], mailbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    return mailbox_warmup.activity(user["id"], mailbox_id)


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a mailbox and its warm-up history")
def delete(mailbox_id: str, user: dict = Depends(get_current_user)):
    if not mailbox_warmup.delete_mailbox(user["id"], mailbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
