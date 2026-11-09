-- Run once in Supabase -> SQL Editor. Replies: the full message and read / unread, like an inbox. Safe to run again.
alter table public.email_replies add column if not exists body text;
alter table public.email_replies add column if not exists read_at timestamptz;
-- Replies older than a day count as read already; newer ones show as unread
update public.email_replies set read_at = now() where read_at is null and received_at < now() - interval '1 day';
