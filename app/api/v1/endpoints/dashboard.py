from fastapi import APIRouter, Depends
from app.api.deps import get_current_user
from app.services.email_service import email_service
from app.core.config import settings

router = APIRouter()


@router.get("/summary", summary="Dashboard summary")
def get_dashboard_summary(user: dict = Depends(get_current_user)):
    stats = email_service.get_stats(user_id=user["id"])
    logs = email_service.get_logs(user_id=user["id"], limit=5)
    return {
        "project": settings.PROJECT_NAME,
        "stats": stats,
        "recent_dispatches": logs,
    }
