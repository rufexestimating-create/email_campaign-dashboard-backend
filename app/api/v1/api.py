from fastapi import APIRouter
from app.api.v1.endpoints import health, emails, dashboard, auth, campaigns, templates, spam, tracking, insights, mailboxes

api_router = APIRouter()

api_router.include_router(health.router, tags=["Health"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(emails.router, prefix="/emails", tags=["Emails"])
api_router.include_router(campaigns.router, prefix="/campaigns", tags=["Campaigns"])
api_router.include_router(campaigns.campaign_router, prefix="/campaign", tags=["Campaigns"])
api_router.include_router(templates.router, prefix="/templates", tags=["Templates"])
api_router.include_router(spam.router, prefix="/spam", tags=["Spam check & knowledge base"])
api_router.include_router(spam.template_router, prefix="/template", tags=["Spam check & knowledge base"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
api_router.include_router(tracking.webhook_router, prefix="/webhooks", tags=["Event tracking"])
api_router.include_router(tracking.router, tags=["Event tracking"])
api_router.include_router(insights.router, prefix="/insights", tags=["Send-time insights"])
api_router.include_router(mailboxes.router, prefix="/mailboxes", tags=["Mailbox warm-up"])
