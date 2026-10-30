import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import resend
from croniter import croniter
from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, status

from app.core.config import settings
from app.db.supabase import get_supabase
from app.schemas.campaign import CampaignCreate
from app.services import email_layout, event_tracking, follow_ups, warmup
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

BATCH_SIZE = 100            # Resend batch API maximum
BATCH_PAUSE_SECONDS = 0.15  # stay under Resend's 10 requests/second
PAGE_SIZE = 1000            # Supabase returns at most 1000 rows per request
MIN_RECURRING_INTERVAL = timedelta(hours=1)
REFRESH_LIMIT = 50
FINAL_EVENTS = ("clicked", "replied", "bounced", "complained")

PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, msg)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise _bad_request(f"Unknown timezone: {tz}")


def upcoming_cron_runs(expr: str, tz: str, count: int = 3) -> list[datetime]:
    """Next `count` run times (UTC) for a cron expression evaluated in the user's timezone."""
    if not croniter.is_valid(expr):
        raise _bad_request("That repeat schedule isn't valid (invalid cron expression). Please choose another time.")
    it = croniter(expr, datetime.now(_zone(tz)))
    runs = [it.get_next(datetime).astimezone(timezone.utc) for _ in range(max(count, 2))]
    if runs[1] - runs[0] < MIN_RECURRING_INTERVAL:
        raise _bad_request("Repeating campaigns can send at most once per hour, so your contacts aren't flooded with emails.")
    return runs[:count]


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", key.strip().lower()).strip("_")


def render(template: str, values: dict, html: bool) -> str:
    """Fills {{placeholders}}; values are HTML-escaped when the template is HTML. Unknown placeholders become empty."""
    return PLACEHOLDER_RE.sub(lambda m: (escape if html else str)(values.get(m.group(1).lower(), "")), template)


GREETING_FALLBACK = "there"  # "Hi {{first_name}}," with no name reads "Hi there," instead of a broken-looking "Hi ,"


def personal_values(r: dict, greeting_fallback: bool = False) -> dict:
    """Placeholder values for one recipient. With greeting_fallback, a missing first name becomes "there" (for message bodies)."""
    name = r.get("name") or ""
    first = name.split()[0] if name else ""
    return {
        **(r.get("variables") or {}),
        "email": r["email"],
        "name": name,
        "first_name": first or (GREETING_FALLBACK if greeting_fallback else ""),
    }


def tidy_subject(subject: str) -> str:
    """Cleans up a subject after an empty placeholder: "Welcome aboard, !" -> "Welcome aboard!", ", it's here" -> "It's here"."""
    s = re.sub(r"\s*,\s*([!?.])", r"\1", subject)       # ", !" left by an empty name before punctuation
    s = re.sub(r",\s*$", "", s)                           # trailing comma
    s = re.sub(r"^\s*[,\-–:]\s*", "", s)                  # leading ", " when the name came first
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:1].upper() + s[1:] if s else s


def unsubscribe_header(c: dict) -> str:
    """List-Unsubscribe header: Gmail and Yahoo show an "Unsubscribe" link next to the sender and trust mail that has it."""
    address = c.get("reply_to") or (re.search(r"[\w.+-]+@[\w.-]+", c["from_email"] or "") or [settings.DEFAULT_FROM_EMAIL])[0]
    return f"<mailto:{address}?subject=unsubscribe>"


class CampaignService:
    # ---------- Create ----------

    def _clean_recipients(self, data: CampaignCreate) -> list[dict]:
        seen, cleaned = set(), []
        for r in data.recipients:
            try:
                email = validate_email(r.email.strip(), check_deliverability=False).normalized.lower()
            except EmailNotValidError:
                continue
            if email in seen:
                continue
            seen.add(email)
            variables = {k: str(v)[:500] for k, v in ((_normalize_key(k), v) for k, v in r.variables.items()) if k}
            cleaned.append({"email": email, "name": (r.name or "").strip() or None, "variables": variables})
        if not cleaned:
            raise _bad_request("No valid recipient email addresses. Please check the list.")
        return cleaned

    def _assign_variants(self, recipients: list[dict], ab_enabled: bool, split: int):
        random.shuffle(recipients)
        a_count = round(len(recipients) * split / 100) if ab_enabled else len(recipients)
        if ab_enabled and len(recipients) >= 2:
            a_count = min(max(a_count, 1), len(recipients) - 1)  # both variants get at least one recipient
        for i, r in enumerate(recipients):
            r["variant"] = "A" if i < a_count else "B"

    def create(self, user_id: str, data: CampaignCreate) -> dict:
        ab = data.ab_test
        if ab.enabled and not (ab.subject_b and ab.subject_b.strip() and ab.body_b and ab.body_b.strip()):
            raise _bad_request("A/B test is on: add a subject line and message for version B.")

        sched = data.schedule
        if data.follow_ups and sched.type == "recurring":
            raise _bad_request("Follow-ups work with one-off campaigns. Turn them off for a repeating campaign.")
        next_run, scheduled_at, cron = None, None, None
        if sched.type == "once":
            if not sched.send_at:
                raise _bad_request("Choose a date and time for the scheduled send.")
            scheduled_at = sched.send_at if sched.send_at.tzinfo else sched.send_at.replace(tzinfo=timezone.utc)
            if scheduled_at < now_utc() - timedelta(minutes=1):
                raise _bad_request("The scheduled time is in the past.")
            next_run = scheduled_at
        elif sched.type == "recurring":
            cron = (sched.cron or "").strip()
            next_run = upcoming_cron_runs(cron, sched.timezone, 2)[0]
        _zone(sched.timezone)

        recipients = self._clean_recipients(data)
        # Seed inboxes (the team's own test addresses) ride along on every campaign, so placement can be checked
        known = {r["email"] for r in recipients}
        recipients += [s for s in warmup.seed_recipients(user_id) if s["email"] not in known]
        self._assign_variants(recipients, ab.enabled, ab.split)

        if data.save_as_draft:
            status_, next_run = "draft", None
        else:
            status_ = "sending" if sched.type == "now" else "scheduled"

        db = get_supabase()
        campaign = db.table("campaigns").insert({
            "user_id": user_id,
            "name": data.name.strip(),
            "from_email": (data.from_email or "").strip() or settings.DEFAULT_FROM_EMAIL,
            "reply_to": data.reply_to,
            "content_type": data.content_type,
            "subject_a": data.subject,
            "body_a": data.body,
            "ab_enabled": ab.enabled,
            "subject_b": ab.subject_b if ab.enabled else None,
            "body_b": ab.body_b if ab.enabled else None,
            "ab_split": ab.split if ab.enabled else 50,
            "schedule_type": sched.type,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "cron_expression": cron,
            "timezone": sched.timezone,
            "next_run_at": next_run.isoformat() if next_run else None,
            "recipient_count": len(recipients),
            "status": status_,
        }).execute().data[0]

        try:
            for i in range(0, len(recipients), 500):
                db.table("campaign_recipients").insert(
                    [{**r, "campaign_id": campaign["id"]} for r in recipients[i:i + 500]]
                ).execute()
            follow_ups.save_steps(user_id, campaign["id"], [f.model_dump() for f in data.follow_ups])
        except Exception:
            db.table("campaigns").delete().eq("id", campaign["id"]).execute()
            raise
        try:
            event_tracking.sync_leads(user_id, [r for r in recipients if not r["variables"].get("seed_inbox")])
        except Exception as e:  # contact profiles are extra; never block a campaign over them
            logger.warning("Could not update leads for campaign %s: %s", campaign["id"], e)
        return campaign

    # ---------- Read ----------

    def _get_owned(self, user_id: str, campaign_id: str) -> dict:
        res = get_supabase().table("campaigns").select("*").eq("id", campaign_id).eq("user_id", user_id).limit(1).execute()
        if not res.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found.")
        return res.data[0]

    def _stats_for(self, campaign_ids: list[str]) -> dict[str, dict]:
        if not campaign_ids:
            return {}
        rows = get_supabase().table("campaign_variant_stats").select("*").in_("campaign_id", campaign_ids).execute().data
        stats: dict[str, dict] = {}
        for row in rows:
            if (row["variant"] or "A") in ("A", "B"):  # follow-up emails (F1, F2) are reported separately
                stats.setdefault(row["campaign_id"], {})[row["variant"] or "A"] = row
        return stats

    def list_for_user(self, user_id: str) -> list[dict]:
        campaigns = (
            get_supabase().table("campaigns")
            .select("id, name, status, schedule_type, scheduled_at, cron_expression, timezone, next_run_at, last_run_at, "
                    "run_count, recipient_count, ab_enabled, ab_split, subject_a, subject_b, last_error, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
        )
        stats = self._stats_for([c["id"] for c in campaigns])
        try:
            follow = follow_ups.summary_for([c["id"] for c in campaigns])
        except Exception as e:  # follow-up counts are extra; the campaign list must still load
            logger.warning("Could not load follow-up counts: %s", e)
            follow = {}
        for c in campaigns:
            c["variant_stats"] = stats.get(c["id"], {})
            c["follow_ups"] = follow.get(c["id"])
        return campaigns

    def detail(self, user_id: str, campaign_id: str) -> dict:
        campaign = self._get_owned(user_id, campaign_id)
        campaign["variant_stats"] = self._stats_for([campaign_id]).get(campaign_id, {})
        campaign["recent_logs"] = (
            get_supabase().table("email_logs")
            .select("id, recipient, subject, status, variant, last_event, error, sent_at")
            .eq("campaign_id", campaign_id)
            .order("sent_at", desc=True)
            .limit(50)
            .execute()
            .data
        )
        return campaign

    # ---------- Actions ----------

    def _claim_for_sending(self, campaign_id: str, from_statuses: list[str]) -> bool:
        """Atomically flips the campaign to 'sending' so two workers can never send it twice."""
        res = (
            get_supabase().table("campaigns")
            .update({"status": "sending", "updated_at": now_utc().isoformat()})
            .eq("id", campaign_id)
            .in_("status", from_statuses)
            .execute()
        )
        return bool(res.data)

    def send_now(self, user_id: str, campaign_id: str) -> str:
        campaign = self._get_owned(user_id, campaign_id)
        if campaign["status"] == "sending":
            raise _bad_request("This campaign is already sending.")
        if not self._claim_for_sending(campaign_id, [campaign["status"]]):
            raise _bad_request("Campaign status changed, refresh and try again.")
        return campaign["status"]

    def pause(self, user_id: str, campaign_id: str):
        campaign = self._get_owned(user_id, campaign_id)
        if campaign["status"] != "scheduled":
            raise _bad_request("Only scheduled campaigns can be paused.")
        get_supabase().table("campaigns").update(
            {"status": "paused", "next_run_at": None, "updated_at": now_utc().isoformat()}
        ).eq("id", campaign_id).execute()

    def resume(self, user_id: str, campaign_id: str):
        """Turns on a campaign's schedule (paused, draft, or a finished recurring campaign)."""
        c = self._get_owned(user_id, campaign_id)
        if c["status"] in ("scheduled", "sending"):
            raise _bad_request("This campaign is already active.")
        if c["schedule_type"] == "recurring":
            next_run = upcoming_cron_runs(c["cron_expression"], c["timezone"], 2)[0]
        elif c["schedule_type"] == "once":
            next_run = datetime.fromisoformat(c["scheduled_at"])
            if next_run < now_utc():
                raise _bad_request("The scheduled time has passed. Use 'Send now' instead.")
        else:
            raise _bad_request("This campaign has no schedule. Use 'Send now' instead.")
        get_supabase().table("campaigns").update(
            {"status": "scheduled", "next_run_at": next_run.isoformat(), "updated_at": now_utc().isoformat()}
        ).eq("id", campaign_id).execute()

    def delete(self, user_id: str, campaign_id: str):
        campaign = self._get_owned(user_id, campaign_id)
        if campaign["status"] == "sending":
            raise _bad_request("Wait for the campaign to finish sending before deleting it.")
        get_supabase().table("campaigns").delete().eq("id", campaign_id).execute()

    # ---------- Sending ----------

    def _load_recipients(self, campaign_id: str) -> list[dict]:
        db, rows, start = get_supabase(), [], 0
        while True:
            page = (
                db.table("campaign_recipients")
                .select("email, name, variables, variant")
                .eq("campaign_id", campaign_id)
                .order("email")
                .range(start, start + PAGE_SIZE - 1)
                .execute()
                .data
            )
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            start += PAGE_SIZE

    def _build_message(self, c: dict, r: dict) -> dict:
        is_b = r["variant"] == "B" and c["ab_enabled"]
        subject = tidy_subject(render(c["subject_b"] if is_b else c["subject_a"], personal_values(r), html=False))
        body_template = c["body_b"] if is_b else c["body_a"]
        is_html = c["content_type"] == "html"
        body = render(body_template, personal_values(r, greeting_fallback=True), html=is_html)
        msg = {
            "from": c["from_email"],
            "to": [r["email"]],
            "subject": subject,
            "html": email_layout.campaign_email(body, c["content_type"], brand=email_layout.brand_name(c["from_email"])),
            "headers": {"List-Unsubscribe": unsubscribe_header(c)},
        }
        if not is_html:
            msg["text"] = body  # plain-text alternative helps deliverability
        if c.get("reply_to"):
            msg["reply_to"] = c["reply_to"]
        return msg

    def _log_rows(self, c: dict, run_recipients: list[dict], messages: list[dict], results: list[tuple]) -> list[dict]:
        sent_at = now_utc().isoformat()
        return [
            {
                "id": email_id or str(uuid.uuid4()),
                "user_id": c["user_id"],
                "campaign_id": c["id"],
                "variant": r["variant"],
                "recipient": r["email"],
                "from_email": c["from_email"],
                "subject": m["subject"],
                "status": status_,
                "error": error,
                "snippet": (m.get("text") or m["html"])[:200],
                "sent_at": sent_at,
            }
            for r, m, (status_, email_id, error) in zip(run_recipients, messages, results)
        ]

    def _send_chunk(self, messages: list[dict]) -> list[tuple]:
        """Returns (status, resend_id, error) per message."""
        if not email_service.is_configured():
            return [("simulated", f"sim_{uuid.uuid4().hex[:10]}", "RESEND_API_KEY not set - simulated") for _ in messages]
        resend.api_key = settings.RESEND_API_KEY
        try:
            res = resend.Batch.send(messages, {"batch_validation": "permissive"})
        except Exception as e:
            return [("failed", None, str(e)) for _ in messages]
        # Permissive mode: `data` holds ids for the accepted emails in order, `errors` lists rejected indexes
        errors = {e["index"]: e["message"] for e in (res.get("errors") or [])}
        ids = iter(res.get("data") or [])
        return [("failed", None, errors[i]) if i in errors else ("sent", next(ids, {}).get("id"), None) for i in range(len(messages))]

    def _deliver(self, c: dict) -> tuple[int, int]:
        recipients = self._load_recipients(c["id"])
        db, sent, failed = get_supabase(), 0, 0
        for i in range(0, len(recipients), BATCH_SIZE):
            chunk = recipients[i:i + BATCH_SIZE]
            messages = [self._build_message(c, r) for r in chunk]
            results = self._send_chunk(messages)
            db.table("email_logs").insert(self._log_rows(c, chunk, messages, results)).execute()
            failed += sum(1 for s, _, _ in results if s == "failed")
            sent += len(results) - sum(1 for s, _, _ in results if s == "failed")
            if i + BATCH_SIZE < len(recipients):
                time.sleep(BATCH_PAUSE_SECONDS)
        return sent, failed

    def run_campaign(self, campaign_id: str, previous_status: str):
        """Sends one run of a campaign that was already claimed (status 'sending'), then sets its next state."""
        db = get_supabase()
        c = db.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute().data[0]
        error, sent = None, 0
        try:
            sent, failed = self._deliver(c)
            if sent == 0 and failed:
                error = f"All {failed} emails failed. Check the activity log for Resend's error."
        except Exception as e:
            error = f"Send aborted: {e}"
            print(f"[campaigns] {campaign_id} failed: {e}", flush=True)

        update = {
            "last_run_at": now_utc().isoformat(),
            "run_count": c["run_count"] + 1,
            "last_error": error,
            "updated_at": now_utc().isoformat(),
        }
        current = db.table("campaigns").select("status").eq("id", campaign_id).limit(1).execute().data
        if not current:
            return  # deleted meanwhile
        if current[0]["status"] == "paused" or (c["schedule_type"] == "recurring" and previous_status == "paused"):
            update["status"] = "paused"
        elif c["schedule_type"] == "recurring" and previous_status == "scheduled":
            try:
                update["status"] = "scheduled"
                update["next_run_at"] = upcoming_cron_runs(c["cron_expression"], c["timezone"], 2)[0].isoformat()
            except HTTPException as e:
                update.update(status="failed", next_run_at=None, last_error=e.detail)
        else:
            update["status"] = "failed" if error else "sent"
            update["next_run_at"] = None
        db.table("campaigns").update(update).eq("id", campaign_id).execute()
        if sent and c["schedule_type"] != "recurring":
            try:
                follow_ups.after_campaign_sent(c)
            except Exception as e:  # never fail a sent campaign over its follow-ups
                logger.warning("Could not queue follow-ups for %s: %s", campaign_id, e)
        print(
            f"[scheduler] '{c['name']}' done: {sent if not error else 0} sent"
            + (f", next send {update['next_run_at']} (UTC)" if update.get("next_run_at") else "")
            + (f", error: {error}" if error else ""),
            flush=True,
        )

    def run_due_campaigns(self):
        """Called by the scheduler loop: sends every scheduled campaign whose time has come."""
        due = (
            get_supabase().table("campaigns")
            .select("id")
            .eq("status", "scheduled")
            .lte("next_run_at", now_utc().isoformat())
            .order("next_run_at")
            .limit(10)
            .execute()
            .data
        )
        for row in due:
            if self._claim_for_sending(row["id"], ["scheduled"]):
                print(f"[scheduler] {now_utc():%H:%M:%S} UTC - campaign {row['id']} is due, sending now", flush=True)
                self.run_campaign(row["id"], "scheduled")

    def recover_interrupted(self):
        """A campaign left in 'sending' after a server restart can't be resumed safely (some emails may be out)."""
        get_supabase().table("campaigns").update({
            "status": "failed",
            "last_error": "Interrupted by a server restart. Check the activity log before re-sending.",
        }).eq("status", "sending").execute()

    # ---------- A/B results ----------

    def refresh_events(self, user_id: str, campaign_id: str) -> int:
        """Pulls the latest delivery/open/click status of sent emails from Resend."""
        self._get_owned(user_id, campaign_id)
        if not email_service.is_configured():
            return 0
        db = get_supabase()
        rows = (
            db.table("email_logs")
            .select("id, last_event")
            .eq("campaign_id", campaign_id)
            .eq("status", "sent")
            .order("sent_at", desc=True)
            .limit(500)
            .execute()
            .data
        )
        pending = [r for r in rows if r["last_event"] not in FINAL_EVENTS][:REFRESH_LIMIT]
        resend.api_key = settings.RESEND_API_KEY
        updated = 0
        for r in pending:
            try:
                event = resend.Emails.get(r["id"]).get("last_event")
            except Exception:
                continue
            if event and event != r["last_event"]:
                db.table("email_logs").update({"last_event": event}).eq("id", r["id"]).execute()
                updated += 1
            time.sleep(0.11)
        return updated


campaign_service = CampaignService()
