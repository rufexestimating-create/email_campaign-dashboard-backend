-- Run this in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Safe to run again: every statement skips what already exists.
-- The FastAPI backend talks to these tables with the service_role key, which bypasses
-- Row Level Security. RLS is enabled with no policies so the public anon key
-- (used by browsers) can never read password hashes or tokens.

create extension if not exists pgcrypto;

-- Users: passwords are stored only as bcrypt hashes
create table if not exists public.users (
  id                  uuid primary key default gen_random_uuid(),
  email               text not null unique,
  full_name           text,
  password_hash       text not null,
  is_verified         boolean not null default false,
  password_changed_at timestamptz not null default now(),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- One-time tokens for email verification and password reset.
-- Only a SHA-256 hash of each token is stored; the raw token lives only in the emailed link.
create table if not exists public.auth_tokens (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id) on delete cascade,
  token_hash text not null unique,
  purpose    text not null check (purpose in ('verify_email', 'reset_password')),
  expires_at timestamptz not null,
  used_at    timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists auth_tokens_user_purpose_idx on public.auth_tokens (user_id, purpose);

-- Email dispatch history, scoped per user
create table if not exists public.email_logs (
  id         text primary key,
  user_id    uuid not null references public.users(id) on delete cascade,
  recipient  text not null,
  from_email text not null,
  subject    text not null,
  status     text not null check (status in ('sent', 'failed', 'simulated')),
  error      text,
  snippet    text,
  sent_at    timestamptz not null default now()
);
create index if not exists email_logs_user_sent_idx on public.email_logs (user_id, sent_at desc);

-- ---------- Campaigns ----------

-- Saved email templates (subject + body), per user
create table if not exists public.email_templates (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references public.users(id) on delete cascade,
  name         text not null,
  subject      text not null,
  body         text not null,
  content_type text not null default 'text' check (content_type in ('text', 'html')),
  created_at   timestamptz not null default now()
);
create index if not exists email_templates_user_idx on public.email_templates (user_id, created_at desc);

-- A campaign: content (with optional B variant for A/B testing) + schedule
create table if not exists public.campaigns (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.users(id) on delete cascade,
  name            text not null,
  from_email      text not null,
  reply_to        text,
  content_type    text not null default 'text' check (content_type in ('text', 'html')),
  subject_a       text not null,
  body_a          text not null,
  ab_enabled      boolean not null default false,
  subject_b       text,
  body_b          text,
  ab_split        int not null default 50 check (ab_split between 1 and 99),  -- % of recipients that get variant A
  schedule_type   text not null check (schedule_type in ('now', 'once', 'recurring')),
  scheduled_at    timestamptz,             -- one-time send time
  cron_expression text,                    -- recurring schedule, e.g. '0 9 * * 1' = Mondays 09:00
  timezone        text not null default 'UTC',
  next_run_at     timestamptz,
  last_run_at     timestamptz,
  run_count       int not null default 0,
  recipient_count int not null default 0,
  status          text not null check (status in ('draft', 'scheduled', 'sending', 'sent', 'paused', 'failed')),
  last_error      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists campaigns_user_idx on public.campaigns (user_id, created_at desc);
create index if not exists campaigns_due_idx on public.campaigns (status, next_run_at);

-- Audience of a campaign (from the paste box / CSV). variables = extra CSV columns for {{placeholders}}
create table if not exists public.campaign_recipients (
  id          uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  email       text not null,
  name        text,
  variables   jsonb not null default '{}'::jsonb,
  variant     text not null default 'A' check (variant in ('A', 'B')),
  unique (campaign_id, email)
);

-- Paced ("drip") sending: each recipient gets its own send time, so a campaign goes out gradually,
-- survives a server restart, and can pause itself if bounces or complaints show up.
alter table public.campaigns add column if not exists send_speed text not null default 'safe';
alter table public.campaigns drop constraint if exists campaigns_send_speed_check;
alter table public.campaigns add constraint campaigns_send_speed_check check (send_speed in ('safe', 'normal', 'fast'));
alter table public.campaigns add column if not exists run_origin text;            -- status the current run started from
alter table public.campaigns add column if not exists run_started_at timestamptz;
alter table public.campaigns add column if not exists paused_reason text;         -- why sending stopped by itself
alter table public.campaign_recipients add column if not exists send_status text not null default 'idle';
alter table public.campaign_recipients drop constraint if exists campaign_recipients_send_status_check;
alter table public.campaign_recipients add constraint campaign_recipients_send_status_check
  check (send_status in ('idle', 'pending', 'sending', 'sent', 'failed'));
alter table public.campaign_recipients add column if not exists send_after timestamptz;
alter table public.campaign_recipients add column if not exists sent_at timestamptz;
alter table public.campaign_recipients add column if not exists send_error text;
create index if not exists campaign_recipients_queue_idx on public.campaign_recipients (campaign_id, send_status, send_after);

-- Link each sent email to its campaign + A/B variant; last_event is synced from Resend (delivered/opened/clicked/...)
alter table public.email_logs add column if not exists campaign_id uuid references public.campaigns(id) on delete set null;
alter table public.email_logs add column if not exists variant     text;
alter table public.email_logs add column if not exists last_event  text;
create index if not exists email_logs_campaign_idx on public.email_logs (campaign_id, variant);

-- Per-campaign, per-variant results used by the dashboard and A/B comparison
create or replace view public.campaign_variant_stats with (security_invoker = true) as
select
  campaign_id,
  variant,
  count(*)                                                          as total,
  count(*) filter (where status = 'sent')                           as sent,
  count(*) filter (where status = 'simulated')                      as simulated,
  count(*) filter (where status = 'failed')                         as failed,
  -- A replied email was delivered and opened, so 'replied' counts in both
  count(*) filter (where last_event in ('delivered', 'opened', 'clicked', 'replied')) as delivered,
  count(*) filter (where last_event in ('opened', 'clicked', 'replied')) as opened,
  count(*) filter (where last_event = 'clicked')                    as clicked,
  count(*) filter (where last_event in ('bounced', 'complained'))   as bounced
from public.email_logs
where campaign_id is not null
group by campaign_id, variant;

-- ---------- Spam knowledge base: keywords each team adds on top of the built-in list ----------
create table if not exists public.spam_keywords (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  phrase      text not null,
  severity    text not null default 'medium' check (severity in ('high', 'medium', 'low')),
  alternative text,
  note        text,
  created_at  timestamptz not null default now(),
  unique (user_id, phrase)
);
alter table public.spam_keywords enable row level security;

-- ---------- Event tracking: leads, every email event, inbox/spam placement labels ----------
-- All timestamps are timestamptz stored in UTC. Read them in the recipient's time with email_events_local.
-- "Sends" are the existing email_logs rows (id = the Resend email id).

-- One profile per contact per user, filled from spreadsheet columns (persona / zip / state) or edited later
create table if not exists public.leads (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id) on delete cascade,
  email      text not null,                 -- always lower case
  name       text,
  company    text,
  persona    text check (persona in ('contractor', 'architect', 'homeowner', 'other')),
  zip        text check (zip ~ '^\d{5}$'),
  state      text check (state ~ '^[A-Z]{2}$'),
  timezone   text,                          -- IANA name, e.g. America/Chicago; derived from ZIP / state
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, email)
);

-- Every event for every sent email, from Resend webhooks (or recorded by hand, e.g. replies)
create table if not exists public.email_events (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  email_id    text not null references public.email_logs(id) on delete cascade,
  campaign_id uuid references public.campaigns(id) on delete set null,
  variant     text,
  recipient   text not null,
  lead_id     uuid references public.leads(id) on delete set null,
  event_type  text not null check (event_type in ('sent', 'delivered', 'delivery_delayed', 'opened', 'clicked', 'replied', 'bounced', 'complained', 'failed')),
  occurred_at timestamptz not null,
  device      text check (device in ('desktop', 'mobile', 'tablet', 'proxy')),  -- proxy = Gmail/Yahoo image proxy hid the device
  user_agent  text,
  link        text,                         -- clicked link
  bounce_type text,                         -- Permanent / Transient
  source      text not null default 'resend_webhook' check (source in ('resend_webhook', 'manual', 'inbox')),  -- inbox = read from the reply mailbox
  svix_id     text unique,                  -- webhook message id: retries are stored once
  data        jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists email_events_user_time_idx on public.email_events (user_id, occurred_at desc);
create index if not exists email_events_email_idx     on public.email_events (email_id);
create index if not exists email_events_recipient_idx on public.email_events (user_id, recipient);

-- Replies read from the reply mailbox (IMAP, app/services/reply_inbox.py). One row per inbox message.
create table if not exists public.email_replies (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  email_id    text not null references public.email_logs(id) on delete cascade,
  campaign_id uuid references public.campaigns(id) on delete set null,
  recipient   text not null,
  from_name   text,
  subject     text,
  snippet     text,
  message_id  text not null unique,
  received_at timestamptz not null,
  created_at  timestamptz not null default now()
);
create index if not exists email_replies_user_time_idx on public.email_replies (user_id, received_at desc);

-- Where emails actually landed: seed-test results, plus complaints and bounces added automatically.
-- These are the training labels for the spam/inbox model.
create table if not exists public.placement_labels (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  email_id    text references public.email_logs(id) on delete set null,
  campaign_id uuid references public.campaigns(id) on delete set null,
  recipient   text not null,
  provider    text not null check (provider in ('gmail', 'outlook', 'yahoo', 'icloud', 'other')),
  placement   text not null check (placement in ('inbox', 'promotions', 'spam', 'missing', 'bounced', 'complained')),
  source      text not null check (source in ('seed_test', 'complaint', 'bounce', 'manual')),
  observed_at timestamptz not null default now(),
  notes       text,
  created_at  timestamptz not null default now()
);
create index if not exists placement_labels_user_idx on public.placement_labels (user_id, observed_at desc);
-- One automatic label per email, however many times Resend retries the webhook
create unique index if not exists placement_labels_auto_idx on public.placement_labels (email_id, source) where source in ('complaint', 'bounce');

-- Events in the recipient's local time (UTC when their timezone is unknown). local_weekday: 1 = Monday … 7 = Sunday
create or replace view public.email_events_local with (security_invoker = true) as
select
  e.id, e.user_id, e.email_id, e.campaign_id, e.variant, e.recipient, e.lead_id, e.event_type,
  e.occurred_at, e.device, e.link, e.source,
  l.persona, l.state,
  coalesce(l.timezone, 'UTC')                                                  as recipient_timezone,
  (e.occurred_at at time zone coalesce(l.timezone, 'UTC'))                     as local_time,
  extract(hour   from e.occurred_at at time zone coalesce(l.timezone, 'UTC'))::int as local_hour,
  extract(isodow from e.occurred_at at time zone coalesce(l.timezone, 'UTC'))::int as local_weekday
from public.email_events e
left join public.leads l on l.user_id = e.user_id and l.email = lower(e.recipient);

-- ---------- M8: pre-send pop-up decisions ----------
-- One row per pop-up: what the preflight said, which advice was taken or ignored, and why a failed check was overridden
create table if not exists public.preflight_decisions (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.users(id) on delete cascade,
  campaign_id     uuid references public.campaigns(id) on delete set null,
  verdict         text not null check (verdict in ('pass', 'warn', 'fail')),
  checks          jsonb not null default '[]'::jsonb,   -- [{id, status, message}] as shown in the pop-up
  accepted        jsonb not null default '[]'::jsonb,   -- check ids whose one-click action was applied
  ignored         jsonb not null default '[]'::jsonb,   -- warn / fail check ids that were left as they were
  overridden      boolean not null default false,
  override_reason text,
  outcome         text not null check (outcome in ('sent', 'scheduled', 'cancelled')),
  created_at      timestamptz not null default now(),
  check (not overridden or length(trim(coalesce(override_reason, ''))) >= 5)
);
create index if not exists preflight_decisions_user_idx on public.preflight_decisions (user_id, created_at desc);

-- ---------- M9: lead lifecycle and scores ----------
-- Stage is set by the team (MQL = marketing-qualified, SQL = sales-qualified); scores are written by the lead scorer
alter table public.leads add column if not exists lifecycle_stage text not null default 'new'
  check (lifecycle_stage in ('new', 'mql', 'sql', 'customer', 'lost'));
alter table public.leads add column if not exists stage_changed_at timestamptz;
alter table public.leads add column if not exists p_reply       double precision check (p_reply between 0 and 1);
alter table public.leads add column if not exists p_mql         double precision check (p_mql between 0 and 1);
alter table public.leads add column if not exists p_sql         double precision check (p_sql between 0 and 1);
alter table public.leads add column if not exists score_band    text check (score_band in ('sales', 'nurture', 'awareness'));
alter table public.leads add column if not exists segment       text;
alter table public.leads add column if not exists scored_at     timestamptz;
alter table public.leads add column if not exists score_version text;
create index if not exists leads_user_band_idx on public.leads (user_id, score_band, p_sql desc);

-- ---------- M10: automated follow-ups ----------
-- Up to two follow-up emails per one-off campaign. Each waits `wait_days` (longer for slow repliers) and goes out at the
-- contact's best send window; it is skipped if they replied, bounced, complained, or moved to SQL / customer / lost / sales-ready.
create table if not exists public.campaign_follow_ups (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  step        int  not null check (step between 1 and 2),
  wait_days   int  not null check (wait_days between 1 and 30),
  subject     text not null,
  body        text not null,
  created_at  timestamptz not null default now(),
  unique (campaign_id, step)
);

-- One row per recipient per follow-up step: when it's due and what happened to it
create table if not exists public.follow_up_sends (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references public.users(id) on delete cascade,
  campaign_id       uuid not null references public.campaigns(id) on delete cascade,
  follow_up_id      uuid not null references public.campaign_follow_ups(id) on delete cascade,
  step              int  not null,
  recipient         text not null,
  anchor_email_id   text references public.email_logs(id) on delete set null,  -- the email this one follows up on
  due_at            timestamptz not null,
  status            text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'skipped', 'failed', 'cancelled')),
  skip_reason       text,
  email_id          text,                                                      -- email_logs id of the follow-up once sent
  sent_at           timestamptz,
  created_at        timestamptz not null default now(),
  unique (follow_up_id, recipient)
);
create index if not exists follow_up_sends_due_idx      on public.follow_up_sends (status, due_at);
create index if not exists follow_up_sends_campaign_idx on public.follow_up_sends (campaign_id, status);

-- ---------- Domain warm-up ----------
-- A new sending domain earns trust by growing volume slowly. The daily limit follows a weekly schedule from start_date;
-- seed inboxes are the team's own test addresses, added to every campaign so placement can be checked and logged.
create table if not exists public.warmup_settings (
  user_id        uuid primary key references public.users(id) on delete cascade,
  enabled        boolean not null default true,
  start_date     date not null default current_date,
  seeds_enabled  boolean not null default true,
  updated_at     timestamptz not null default now()
);
create table if not exists public.seed_inboxes (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id) on delete cascade,
  email      text not null,
  provider   text not null check (provider in ('gmail', 'outlook', 'yahoo', 'icloud', 'other')),
  created_at timestamptz not null default now(),
  unique (user_id, email)
);
alter table public.warmup_settings enable row level security;
alter table public.seed_inboxes    enable row level security;

-- ---------- Mailbox warm-up ----------
-- The team's own mailboxes (e.g. Hostinger: ali@, sara@) email each other a few times a day, open each other's mail,
-- move it out of spam and reply, so each mailbox builds a sending reputation before it emails clients.
create table if not exists public.mailboxes (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references public.users(id) on delete cascade,
  email              text not null,
  display_name       text,
  smtp_host          text not null,
  smtp_port          int  not null default 465,
  imap_host          text not null,
  imap_port          int  not null default 993,
  username           text not null,
  password_enc       text not null,              -- encrypted on the server (MAILBOX_SECRET_KEY), never plain text
  status             text not null default 'active' check (status in ('active', 'paused', 'error')),
  warmup_started_on  date not null default current_date,
  daily_limit        int  not null default 30 check (daily_limit between 2 and 50),
  last_error         text,
  last_checked_at    timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (user_id, email)
);

create table if not exists public.warmup_messages (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  from_mailbox_id  uuid not null references public.mailboxes(id) on delete cascade,
  to_mailbox_id    uuid not null references public.mailboxes(id) on delete cascade,
  kind             text not null check (kind in ('initial', 'reply')),
  parent_id        uuid references public.warmup_messages(id) on delete cascade,
  plan_date        date not null,
  subject          text not null,
  body             text not null,
  message_id       text,
  scheduled_at     timestamptz not null,
  status           text not null default 'scheduled' check (status in ('scheduled', 'sent', 'failed', 'cancelled')),
  sent_at          timestamptz,
  error            text,
  landed_in        text check (landed_in in ('inbox', 'spam')),
  found_at         timestamptz,
  rescued          boolean not null default false,
  created_at       timestamptz not null default now()
);
create index if not exists warmup_messages_due_idx  on public.warmup_messages (status, scheduled_at);
create index if not exists warmup_messages_from_idx on public.warmup_messages (from_mailbox_id, plan_date);
create index if not exists warmup_messages_to_idx   on public.warmup_messages (to_mailbox_id, status);

alter table public.mailboxes       enable row level security;
alter table public.warmup_messages enable row level security;

-- Mailbox chat: the team's warm-up mailboxes write to each other from the dashboard, one to one or in a group.
-- Every message is a real email sent over the mailbox's own SMTP (so it counts for the warm-up); the inbox check
-- finds it in each recipient's Inbox or Spam, marks it read and rescues it from spam.
create table if not exists public.chat_threads (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  kind        text not null check (kind in ('direct', 'group')),
  title       text,                              -- groups only
  pair_key    text,                              -- direct only: the two mailbox ids, sorted, so a pair has one chat
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, pair_key)
);
create table if not exists public.chat_members (
  thread_id   uuid not null references public.chat_threads(id) on delete cascade,
  mailbox_id  uuid not null references public.mailboxes(id) on delete cascade,
  primary key (thread_id, mailbox_id)
);
create table if not exists public.chat_messages (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  thread_id        uuid not null references public.chat_threads(id) on delete cascade,
  from_mailbox_id  uuid not null references public.mailboxes(id) on delete cascade,
  subject          text not null,
  body             text not null,
  quote            text,                         -- a private reply to a group message quotes it
  message_id       text,
  created_at       timestamptz not null default now()
);
create table if not exists public.chat_receipts (
  message_id  uuid not null references public.chat_messages(id) on delete cascade,
  mailbox_id  uuid not null references public.mailboxes(id) on delete cascade,
  landed_in   text check (landed_in in ('inbox', 'spam')),
  rescued     boolean not null default false,
  found_at    timestamptz,
  primary key (message_id, mailbox_id)
);
create index if not exists chat_members_mailbox_idx on public.chat_members (mailbox_id);
create index if not exists chat_messages_thread_idx on public.chat_messages (thread_id, created_at);

-- Newsletters a mailbox was subscribed to (by hand, from the dashboard's list). The inbox check confirms the
-- subscription, opens each newsletter and moves it out of spam, so the mailbox gets ordinary outside mail.
create table if not exists public.newsletter_subscriptions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  mailbox_id       uuid not null references public.mailboxes(id) on delete cascade,
  name             text not null,
  signup_url       text,
  sender_domains   text not null,                -- comma-separated, e.g. 'morningbrew.com'
  status           text not null default 'pending' check (status in ('pending', 'confirmed')),
  subscribed_at    timestamptz not null default now(),
  confirmed_at     timestamptz,
  last_received_at timestamptz,
  received_count   int not null default 0,
  rescued_count    int not null default 0,
  unique (mailbox_id, name)
);

alter table public.chat_threads             enable row level security;
alter table public.chat_members             enable row level security;
alter table public.chat_messages            enable row level security;
alter table public.chat_receipts            enable row level security;
alter table public.newsletter_subscriptions enable row level security;

alter table public.leads               enable row level security;
alter table public.email_events        enable row level security;
alter table public.email_replies       enable row level security;
alter table public.placement_labels    enable row level security;
alter table public.preflight_decisions enable row level security;
alter table public.campaign_follow_ups enable row level security;
alter table public.follow_up_sends     enable row level security;

alter table public.users               enable row level security;
alter table public.auth_tokens         enable row level security;
alter table public.email_logs          enable row level security;
alter table public.email_templates     enable row level security;
alter table public.campaigns           enable row level security;
alter table public.campaign_recipients enable row level security;
