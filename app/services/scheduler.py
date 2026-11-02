import asyncio

from app.core.config import settings
from app.db.supabase import is_supabase_configured
from app.services import follow_ups, reply_inbox
from app.services.campaign_service import campaign_service

CHECK_INTERVAL_SECONDS = 30
QUEUE_INTERVAL_SECONDS = 5  # paced sends are 5-30 s apart, so the queue is checked more often than the schedule


async def _send_queue():
    """Sends the queued emails of running campaigns as their send times come (see campaign_service.process_queue)."""
    while True:
        try:
            await asyncio.to_thread(campaign_service.process_queue)
        except Exception as e:
            print(f"[scheduler] send queue error: {e}", flush=True)
        await asyncio.sleep(QUEUE_INTERVAL_SECONDS)


async def campaign_scheduler():
    """Cron-style loop: every 30s, reads new replies (at most once a minute), starts scheduled and recurring campaigns
    that are due, then sends due follow-ups.
    A second loop sends each running campaign's queued emails. Runs while the API is up."""
    if not is_supabase_configured():
        print("[scheduler] Supabase not configured - campaign scheduler disabled", flush=True)
        return
    if not settings.RUN_SCHEDULER:
        print("[scheduler] RUN_SCHEDULER=false - this copy sends nothing; the live server does the sending", flush=True)
        return
    for recover in (campaign_service.recover_interrupted, follow_ups.recover_interrupted):
        try:
            await asyncio.to_thread(recover)
        except Exception as e:
            print(f"[scheduler] startup recovery failed: {e}", flush=True)

    queue = asyncio.create_task(_send_queue())
    try:
        while True:
            # Replies first, so a reply that just arrived stops that person's follow-up in the same pass
            jobs = ((reply_inbox.poll, "replies"), (campaign_service.run_due_campaigns, "campaigns"), (follow_ups.run_due, "follow-ups"))
            for job, name in jobs:
                try:
                    await asyncio.to_thread(job)
                except Exception as e:
                    print(f"[scheduler] {name} error: {e}", flush=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        queue.cancel()
