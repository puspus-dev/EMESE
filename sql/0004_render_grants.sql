-- Render / sima Postgres: RLS és Supabase szerepkörök nélkül
alter table if exists public.stops disable row level security;
alter table if exists public.routes disable row level security;
alter table if exists public.trips disable row level security;
alter table if exists public.stop_times disable row level security;
alter table if exists public.calendar disable row level security;
alter table if exists public.calendar_dates disable row level security;

grant usage on schema public to public;
grant select on all tables in schema public to public;
grant execute on all functions in schema public to public;
