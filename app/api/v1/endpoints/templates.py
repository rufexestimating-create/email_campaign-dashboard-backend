from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.db.supabase import get_supabase
from app.schemas.campaign import TemplateCreate

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("", summary="List saved templates")
def list_templates(user: dict = Depends(get_current_user)):
    return (
        get_supabase().table("email_templates")
        .select("id, name, subject, body, content_type, created_at")
        .eq("user_id", user["id"])
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Save a template")
def create_template(data: TemplateCreate, user: dict = Depends(get_current_user)):
    return get_supabase().table("email_templates").insert({**data.model_dump(), "user_id": user["id"]}).execute().data[0]


@router.delete("/{template_id}", summary="Delete a template")
def delete_template(template_id: str, user: dict = Depends(get_current_user)):
    res = get_supabase().table("email_templates").delete().eq("id", template_id).eq("user_id", user["id"]).execute()
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found.")
    return {"message": "Template deleted."}
