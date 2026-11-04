from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.services import mailbox_chat

router = APIRouter(dependencies=[Depends(get_current_user)])


class DirectOpen(BaseModel):
    mailbox_id: str
    with_mailbox_id: str


class GroupCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    mailbox_ids: list[str] = Field(min_length=2, max_length=20)


class MessageSend(BaseModel):
    from_mailbox_id: str
    body: str = Field(min_length=1, max_length=mailbox_chat.MAX_BODY)
    quote: Optional[str] = Field(None, max_length=mailbox_chat.MAX_BODY)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/threads", summary="A mailbox's conversations: its groups and a private chat with each other mailbox")
def threads(mailbox_id: str, user: dict = Depends(get_current_user)):
    return _run(mailbox_chat.list_threads, user["id"], mailbox_id)


@router.post("/direct", summary="Open (or start) the private chat between two mailboxes")
def direct(data: DirectOpen, user: dict = Depends(get_current_user)):
    return _run(mailbox_chat.open_direct, user["id"], data.mailbox_id, data.with_mailbox_id)


@router.post("/groups", status_code=status.HTTP_201_CREATED, summary="Start a group chat between several mailboxes")
def group(data: GroupCreate, user: dict = Depends(get_current_user)):
    return _run(mailbox_chat.create_group, user["id"], data.title, data.mailbox_ids)


@router.get("/threads/{thread_id}/messages", summary="A conversation's messages and where each one landed")
def messages(thread_id: str, user: dict = Depends(get_current_user)):
    return _run(mailbox_chat.messages, user["id"], thread_id)


@router.post("/threads/{thread_id}/messages", status_code=status.HTTP_201_CREATED, summary="Send a message (a real email) now")
def send(thread_id: str, data: MessageSend, user: dict = Depends(get_current_user)):
    return _run(mailbox_chat.send, user["id"], thread_id, data.from_mailbox_id, data.body, data.quote)
