import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from app.core.config import settings
from app.api.v1.api import api_router
from app.services import placement_model
from app.services.scheduler import campaign_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background loop that sends scheduled / recurring campaigns
    scheduler = asyncio.create_task(campaign_scheduler())
    # Load the placement model off the event loop, so startup isn't held up and the first spam check is fast
    warm_up = asyncio.create_task(asyncio.to_thread(placement_model.warm_up)) if settings.SPAM_DETECTOR_ENABLED else None
    yield
    scheduler.cancel()
    if warm_up:
        warm_up.cancel()


app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    description="FastAPI backend for Resend Email Dashboard with Hostinger Domain support",
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
)

# Set up CORS middleware so Next.js frontend can communicate seamlessly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For local development convenience
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

# Missing table / column codes from Supabase (PostgREST + Postgres)
SCHEMA_ERROR_CODES = {"PGRST205", "PGRST204", "42P01", "42703"}


@app.exception_handler(APIError)
async def supabase_error_handler(request: Request, exc: APIError):
    # Registered for a specific exception type so the response still passes through CORS
    # and the browser can read the message instead of reporting a network failure.
    if exc.code in SCHEMA_ERROR_CODES:
        detail = "Database setup incomplete: run backend/supabase_schema.sql in the Supabase SQL Editor."
        return JSONResponse(status_code=503, content={"detail": detail})
    return JSONResponse(status_code=502, content={"detail": f"Database error: {exc.message}"})


@app.get("/")
def root():
    return {
        "message": "Resend Email Dashboard API is running",
        "docs": f"{settings.API_V1_STR}/docs",
        "health": f"{settings.API_V1_STR}/health",
    }
