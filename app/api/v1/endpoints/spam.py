from fastapi import APIRouter, Depends, HTTPException, status
from postgrest.exceptions import APIError

from app.api.deps import get_current_user, spam_detector_on
from app.db.supabase import get_supabase
from app.schemas.campaign import SpamCheckBatchRequest, SpamCheckRequest, SpamKeywordCreate
from app.services import placement_model, spam_checker, spam_knowledge


router = APIRouter(dependencies=[Depends(get_current_user), Depends(spam_detector_on)])
template_router = APIRouter(dependencies=[Depends(get_current_user), Depends(spam_detector_on)])


@router.post("/check", summary="Spam risk score for a subject + body")
@template_router.post("/score", summary="Inbox score 0-100 (rules + placement model), risk level, top reasons, highlight positions and one-click fixes")
def check(data: SpamCheckRequest, user: dict = Depends(get_current_user)):
    return placement_model.score(data.subject, data.body, data.content_type, user["id"])


@router.post("/check-batch", summary="Spam scores for several emails (e.g. template cards)")
def check_batch(data: SpamCheckBatchRequest, user: dict = Depends(get_current_user)):
    return [
        {k: v for k, v in placement_model.score(i.subject, i.body, i.content_type, user["id"]).items() if k in ("score", "level", "label")}
        for i in data.items
    ]


@router.get("/knowledge", summary="Spam keyword knowledge base, audience guide and best practices")
def knowledge(user: dict = Depends(get_current_user)):
    return {
        "categories": [{"id": c["id"], "name": c["name"], "why": c["why"], "industry": c.get("industry", False)} for c in spam_knowledge.CATEGORIES],
        "keywords": spam_knowledge.builtin_keywords() + spam_checker.custom_keywords(user["id"]),
        "audience_guide": spam_knowledge.AUDIENCE_GUIDE,
        "vocabulary": spam_knowledge.RECOMMENDED_VOCABULARY,
        "best_practices": spam_knowledge.BEST_PRACTICES,
    }


@router.post("/keywords", status_code=status.HTTP_201_CREATED, summary="Add your own spam keyword")
def add_keyword(data: SpamKeywordCreate, user: dict = Depends(get_current_user)):
    row = {**data.model_dump(), "phrase": data.phrase.strip().lower(), "user_id": user["id"]}
    try:
        return get_supabase().table("spam_keywords").insert(row).execute().data[0]
    except APIError as e:
        if e.code == "23505":  # unique (user_id, phrase)
            raise HTTPException(status.HTTP_409_CONFLICT, "That keyword is already in your list.")
        raise


@router.delete("/keywords/{keyword_id}", summary="Delete one of your keywords")
def delete_keyword(keyword_id: str, user: dict = Depends(get_current_user)):
    res = get_supabase().table("spam_keywords").delete().eq("id", keyword_id).eq("user_id", user["id"]).execute()
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Keyword not found.")
    return {"message": "Keyword deleted."}
