-- Run once in Supabase -> SQL Editor. Per-mailbox warm-up settings like Instantly's. Safe to run again.
-- Empty = the standard ramp (3, 6, 12, 20 a day) and a 50% reply rate.
alter table public.mailboxes add column if not exists warmup_increase int check (warmup_increase between 1 and 10);
alter table public.mailboxes add column if not exists warmup_reply_rate int check (warmup_reply_rate between 0 and 100);
