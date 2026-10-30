import asyncio

from app.db.supabase import is_supabase_configured
from app.services import follow_ups
from app.services.campaign_service import campaign_service

CHECK_INTERVAL_SECONDS = 30


async def campaign_scheduler():
    """Cron-style loop: every 30s, sends scheduled and recurring campaigns that are due, then due follow-ups. Runs while the API is up."""
    if not is_supabase_configured():
        print("[scheduler] Supabase not configured - campaign scheduler disabled", flush=True)
        return
    for recover in (campaign_service.recover_interrupted, follow_ups.recover_interrupted):
        try:
            await asyncio.to_thread(recover)
        except Exception as e:
            print(f"[scheduler] startup recovery failed: {e}", flush=True)

    while True:
        for job, name in ((campaign_service.run_due_campaigns, "campaigns"), (follow_ups.run_due, "follow-ups")):
            try:
                await asyncio.to_thread(job)
            except Exception as e:
                print(f"[scheduler] {name} error: {e}", flush=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
