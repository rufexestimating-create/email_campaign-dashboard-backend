-- Run once in Supabase -> SQL Editor. Advanced warm-up settings like Instantly's (open rate, spam protection, ...). Safe to run again.
alter table public.mailboxes add column if not exists warmup_settings jsonb not null default '{}'::jsonb;
