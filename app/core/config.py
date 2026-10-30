import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Resend Email Dashboard API"
    API_V1_STR: str = "/api/v1"
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Resend API Configuration
    # If using your Hostinger domain, set DEFAULT_FROM_EMAIL to your address, e.g. hello@yourdomain.com
    RESEND_API_KEY: str = ""
    DEFAULT_FROM_EMAIL: str = "onboarding@resend.dev"
    # Signing secret of the webhook endpoint (Resend -> Webhooks -> your endpoint), starts with whsec_
    RESEND_WEBHOOK_SECRET: str = ""

    # Emails your Resend plan allows per day (free plan: 100). Used by the pre-send checks.
    DAILY_SEND_LIMIT: int = 100

    # Email design: company name in the email header/footer and the accent colour
    BRAND_NAME: str = "Arvistry"
    # CADTRI orange, deep enough for white button text in emails (5.2:1)
    BRAND_COLOR: str = "#C2410C"

    # Supabase (Project Settings -> API). The service_role key must stay on the backend only.
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # JWT authentication. Generate a secret with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # Links in verification / reset emails point here
    FRONTEND_URL: str = "http://localhost:3000"
    VERIFY_EMAIL_TOKEN_EXPIRE_HOURS: int = 24
    RESET_PASSWORD_TOKEN_EXPIRE_MINUTES: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )


settings = Settings()
