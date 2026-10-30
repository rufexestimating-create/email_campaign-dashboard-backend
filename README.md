---
title: Email Dashboard Backend
emoji: 📬
colorFrom: yellow
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

# Email Dashboard Backend

FastAPI backend for the email dashboard: campaigns, follow-ups, spam checks and insights.

- Health check: `/api/v1/health`
- API docs: `/api/v1/docs`
- Settings: see `.env.example`. On Hugging Face, add them under Space Settings -> Variables and secrets.

Run locally: `python run.py` (port 8000).
