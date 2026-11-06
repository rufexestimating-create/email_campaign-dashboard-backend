-- Run once in Supabase -> SQL Editor. "Weekdays only" warm-up, like Instantly. Safe to run again.
alter table public.mailboxes add column if not exists warmup_weekdays_only boolean not null default false;
