from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
from app.schemas.mailbox import MailboxCreate, MailboxUpdate
from app.services import mailbox_warmup
from app.services.mailbox_warmup import MailboxError

router = APIRouter(dependencies=[Depends(get_current_user)])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("", summary="Warm-up mailboxes with today's progress and where their emails landed")
def overview(user: dict = Depends(get_current_user)):
    return mailbox_warmup.overview(user["id"])


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
