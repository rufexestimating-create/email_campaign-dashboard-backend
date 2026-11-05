-- Run once in Supabase -> SQL Editor. Allows a follow-up "10 minutes (for testing)" (wait_days = 0). Safe to run again.
alter table public.campaign_follow_ups drop constraint if exists campaign_follow_ups_wait_days_check;
alter table public.campaign_follow_ups add constraint campaign_follow_ups_wait_days_check check (wait_days between 0 and 30);
