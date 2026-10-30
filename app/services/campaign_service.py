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
from app.services import email_layout, event_tracking, follow_ups, pacing, warmup
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

BATCH_SIZE = 100            # Resend batch API maximum
BATCH_PAUSE_SECONDS = 0.15  # stay under Resend's 10 requests/second
MAX_BATCHES_PER_TICK = 10   # 'fast' campaigns: at most 1,000 emails per queue pass
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
            "send_speed": data.send_speed,
            "run_origin": "draft" if status_ == "sending" else None,
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
                    "run_count, recipient_count, ab_enabled, ab_split, subject_a, subject_b, last_error, created_at, "
                    "send_speed, paused_reason")
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
            c["progress"] = self.progress(c["id"]) if c["status"] in ("sending", "paused") else None
        return campaigns

    def progress(self, campaign_id: str) -> dict | None:
        """Where the current run stands: emails sent, failed and still queued, and when the next one goes out.
        None when the campaign has no run in progress."""
        db = get_supabase()
        counts = {s: self._count_recipients(campaign_id, [s]) for s in ("pending", "sending", "sent", "failed")}
        queued = counts["pending"] + counts["sending"]
        if not queued and not counts["sent"] and not counts["failed"]:
            return None
        nxt = (db.table("campaign_recipients").select("send_after").eq("campaign_id", campaign_id)
               .eq("send_status", "pending").order("send_after").limit(1).execute().data)
        return {
            "total": queued + counts["sent"] + counts["failed"],
            "sent": counts["sent"],
            "failed": counts["failed"],
            "queued": queued,
            "next_send_at": nxt[0]["send_after"] if nxt else None,
        }

    def detail(self, user_id: str, campaign_id: str) -> dict:
        campaign = self._get_owned(user_id, campaign_id)
        campaign["variant_stats"] = self._stats_for([campaign_id]).get(campaign_id, {})
        campaign["progress"] = self.progress(campaign_id) if campaign["status"] in ("sending", "paused") else None
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

    def _claim_for_sending(self, campaign_id: str, from_status: str) -> bool:
        """Atomically flips the campaign to 'sending' so two workers can never send it twice. The run isn't queued yet
        (run_started_at is cleared); start_run does that."""
        res = (
            get_supabase().table("campaigns")
            .update({"status": "sending", "run_origin": from_status, "run_started_at": None, "paused_reason": None,
                     "updated_at": now_utc().isoformat()})
            .eq("id", campaign_id)
            .eq("status", from_status)
            .execute()
        )
        return bool(res.data)

    def _count_recipients(self, campaign_id: str, statuses: list[str]) -> int:
        res = (get_supabase().table("campaign_recipients").select("id", count="exact")
               .eq("campaign_id", campaign_id).in_("send_status", statuses).limit(1).execute())
        return res.count or 0

    def _has_unsent(self, campaign_id: str) -> bool:
        return self._count_recipients(campaign_id, ["pending", "sending"]) > 0

    def send_now(self, user_id: str, campaign_id: str):
        campaign = self._get_owned(user_id, campaign_id)
        if campaign["status"] == "sending":
            raise _bad_request("This campaign is already sending.")
        if campaign["status"] == "paused" and self._has_unsent(campaign_id):
            raise _bad_request("This campaign stopped partway through. Use Resume to send the rest, so nobody gets it twice.")
        if not self._claim_for_sending(campaign_id, campaign["status"]):
            raise _bad_request("Campaign status changed, refresh and try again.")

    def pause(self, user_id: str, campaign_id: str):
        campaign = self._get_owned(user_id, campaign_id)
        if campaign["status"] == "sending":
            # Mid-run: the emails still queued wait until Resume
            get_supabase().table("campaigns").update(
                {"status": "paused", "paused_reason": None, "updated_at": now_utc().isoformat()}
            ).eq("id", campaign_id).eq("status", "sending").execute()
            return
        if campaign["status"] != "scheduled":
            raise _bad_request("Only scheduled or sending campaigns can be paused.")
        get_supabase().table("campaigns").update(
            {"status": "paused", "next_run_at": None, "updated_at": now_utc().isoformat()}
        ).eq("id", campaign_id).execute()

    def resume(self, user_id: str, campaign_id: str) -> str:
        """Carries on a run that was paused partway (returns 'sending'), or turns on the campaign's schedule
        (paused, draft, or a finished recurring campaign; returns 'scheduled')."""
        c = self._get_owned(user_id, campaign_id)
        if c["status"] in ("scheduled", "sending"):
            raise _bad_request("This campaign is already active.")
        if c["status"] == "paused" and self._has_unsent(campaign_id):
            self._resume_run(c)
            return "sending"
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
        return "scheduled"

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
                .select("id, email, name, variables, variant")
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

    # ---------- Paced sending ----------
    # A run is queued by start_run (every recipient gets a send time spaced by the campaign's speed) and sent by
    # process_queue, which the scheduler calls every few seconds. Because the queue lives in the database, a run
    # carries on after a server restart, can be paused and resumed partway, and stops itself if the list goes bad.

    def _queue(self, campaign_id: str, recipients: list[dict], speed: str):
        """Gives each recipient a send time from now, spaced by `speed`, and marks it pending."""
        times = pacing.send_times(len(recipients), speed, now_utc())
        rows = [{"id": r["id"], "campaign_id": campaign_id, "email": r["email"], "send_status": "pending",
                 "send_after": t.isoformat(), "sent_at": None, "send_error": None} for r, t in zip(recipients, times)]
        db = get_supabase()
        for i in range(0, len(rows), 500):
            db.table("campaign_recipients").upsert(rows[i:i + 500], on_conflict="id").execute()

    def start_run(self, campaign_id: str):
        """Queues one run of a campaign already claimed for sending (status 'sending')."""
        db = get_supabase()
        rows = db.table("campaigns").select("id, status, send_speed").eq("id", campaign_id).limit(1).execute().data
        if not rows or rows[0]["status"] != "sending":
            return
        recipients = self._load_recipients(campaign_id)
        random.shuffle(recipients)  # A and B recipients spread over the whole run, not one version first
        self._queue(campaign_id, recipients, rows[0].get("send_speed") or pacing.DEFAULT_SPEED)
        # Only now is the run visible to process_queue, so it can't finish a run whose queue isn't written yet
        db.table("campaigns").update({"run_started_at": now_utc().isoformat(), "last_error": None,
                                      "updated_at": now_utc().isoformat()}).eq("id", campaign_id).execute()

    def _resume_run(self, c: dict):
        """Re-spaces the emails still queued from now, so a resumed run doesn't fire its backlog in one burst.
        Bounces and complaints are counted afresh from here on."""
        db, rows, start = get_supabase(), [], 0
        while True:
            page = (db.table("campaign_recipients").select("id, email").eq("campaign_id", c["id"])
                    .in_("send_status", ["pending", "sending"]).order("send_after").range(start, start + PAGE_SIZE - 1).execute().data)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
        self._queue(c["id"], rows, c.get("send_speed") or pacing.DEFAULT_SPEED)
        db.table("campaigns").update({"status": "sending", "paused_reason": None, "run_started_at": now_utc().isoformat(),
                                      "updated_at": now_utc().isoformat()}).eq("id", c["id"]).eq("status", "paused").execute()

    def _count_logs(self, campaign_id: str, since: str, **filters) -> int:
        q = (get_supabase().table("email_logs").select("id", count="exact").eq("campaign_id", campaign_id)
             .in_("variant", ["A", "B"]).gte("sent_at", since))  # follow-ups (F1, F2) are judged on their own
        for column, value in filters.items():
            q = q.eq(column, value)
        return q.limit(1).execute().count or 0

    def _auto_pause_reason(self, c: dict) -> str | None:
        since = c["run_started_at"]
        sent = self._count_logs(c["id"], since, status="sent")
        if not sent:
            return None
        return pacing.pause_reason(sent, self._count_logs(c["id"], since, last_event="bounced"),
                                   self._count_logs(c["id"], since, last_event="complained"))

    def _send_due(self, c: dict, now: datetime) -> int:
        """Sends this campaign's recipients whose time has come. Returns how many went out."""
        db, sent = get_supabase(), 0
        for _ in range(MAX_BATCHES_PER_TICK):
            due = (db.table("campaign_recipients").select("id").eq("campaign_id", c["id"]).eq("send_status", "pending")
                   .lte("send_after", now.isoformat()).order("send_after").limit(BATCH_SIZE).execute().data)
            if not due:
                break
            # Claim first (pending -> sending), so a second worker can never send the same email
            chunk = (db.table("campaign_recipients").update({"send_status": "sending"})
                     .in_("id", [r["id"] for r in due]).eq("send_status", "pending").execute().data)
            if not chunk:
                break
            messages = [self._build_message(c, r) for r in chunk]
            results = self._send_chunk(messages)
            db.table("email_logs").insert(self._log_rows(c, chunk, messages, results)).execute()
            stamp = now_utc().isoformat()
            ok = [r["id"] for r, (s, _, _) in zip(chunk, results) if s != "failed"]
            if ok:
                db.table("campaign_recipients").update({"send_status": "sent", "sent_at": stamp}).in_("id", ok).execute()
            for r, (s, _, error) in zip(chunk, results):
                if s == "failed":
                    db.table("campaign_recipients").update({"send_status": "failed", "sent_at": stamp,
                                                            "send_error": (error or "")[:500]}).eq("id", r["id"]).execute()
            sent += len(ok)
            if len(due) < BATCH_SIZE:
                break
            time.sleep(BATCH_PAUSE_SECONDS)
        return sent

    def _finish_run(self, c: dict):
        """The run's queue is empty: record it and set what the campaign does next."""
        db = get_supabase()
        sent, failed = self._count_recipients(c["id"], ["sent"]), self._count_recipients(c["id"], ["failed"])
        error = f"All {failed} emails failed. Check the activity log for Resend's error." if failed and not sent else None
        update = {"last_run_at": now_utc().isoformat(), "run_count": c["run_count"] + 1, "last_error": error,
                  "updated_at": now_utc().isoformat()}
        origin = c.get("run_origin") or "draft"
        if c["schedule_type"] == "recurring" and origin == "paused":
            update["status"] = "paused"
        elif c["schedule_type"] == "recurring" and origin == "scheduled":
            try:
                update["status"] = "scheduled"
                update["next_run_at"] = upcoming_cron_runs(c["cron_expression"], c["timezone"], 2)[0].isoformat()
            except HTTPException as e:
                update.update(status="failed", next_run_at=None, last_error=e.detail)
        else:
            update["status"] = "failed" if error else "sent"
            update["next_run_at"] = None
        # Only if it's still sending: a pause pressed at the last moment wins
        if not db.table("campaigns").update(update).eq("id", c["id"]).eq("status", "sending").execute().data:
            return
        if sent and c["schedule_type"] != "recurring":
            try:
                follow_ups.after_campaign_sent(c)
            except Exception as e:  # never fail a sent campaign over its follow-ups
                logger.warning("Could not queue follow-ups for %s: %s", c["id"], e)
        print(
            f"[scheduler] '{c['name']}' done: {sent} sent, {failed} failed"
            + (f", next send {update['next_run_at']} (UTC)" if update.get("next_run_at") else ""),
            flush=True,
        )

    def process_queue(self, now: datetime | None = None) -> int:
        """Called by the scheduler every few seconds: sends what's due in every running campaign, pauses a run whose
        bounces or complaints pass the limits, and closes runs with nothing left to send. Returns emails sent."""
        now, total = now or now_utc(), 0
        running = get_supabase().table("campaigns").select("*").eq("status", "sending").execute().data
        for c in running:
            if not c.get("run_started_at"):
                continue  # claimed, queue still being written
            try:
                reason = self._auto_pause_reason(c)
                if reason:
                    get_supabase().table("campaigns").update(
                        {"status": "paused", "paused_reason": reason, "updated_at": now_utc().isoformat()}
                    ).eq("id", c["id"]).eq("status", "sending").execute()
                    print(f"[scheduler] '{c['name']}' paused: {reason}", flush=True)
                    continue
                sent = self._send_due(c, now)
                total += sent
                if not sent and not self._has_unsent(c["id"]):
                    self._finish_run(c)
            except Exception as e:
                print(f"[scheduler] queue error for campaign {c['id']}: {e}", flush=True)
        return total

    def run_due_campaigns(self):
        """Called by the scheduler loop: starts every scheduled campaign whose time has come."""
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
            if self._claim_for_sending(row["id"], "scheduled"):
                print(f"[scheduler] {now_utc():%H:%M:%S} UTC - campaign {row['id']} is due, queueing its emails", flush=True)
                self.start_run(row["id"])

    def recover_interrupted(self):
        """After a restart. Emails claimed but never confirmed may or may not have gone out, so they're marked failed
        rather than risk sending them twice; campaigns claimed but not yet queued get queued; the rest carry on."""
        db = get_supabase()
        db.table("campaign_recipients").update({
            "send_status": "failed",
            "send_error": "Interrupted by a server restart. It may or may not have been sent; check the activity log.",
        }).eq("send_status", "sending").execute()
        for row in db.table("campaigns").select("id, run_started_at").eq("status", "sending").execute().data:
            if not row["run_started_at"]:
                self.start_run(row["id"])

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
