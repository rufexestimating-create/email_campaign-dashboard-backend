-- Run once in Supabase -> SQL Editor. Adds the mailbox chat and newsletter tables (safe to run again).

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
