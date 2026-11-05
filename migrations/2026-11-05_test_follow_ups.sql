-- Run once in Supabase -> SQL Editor. Short follow-up waits (1 minute, 10 minutes, 1 hour). Safe to run again.
alter table public.campaign_follow_ups drop constraint if exists campaign_follow_ups_wait_days_check;
alter table public.campaign_follow_ups add constraint campaign_follow_ups_wait_days_check check (wait_days between 0 and 30);
alter table public.campaign_follow_ups add column if not exists wait_minutes int check (wait_minutes between 1 and 1440);
