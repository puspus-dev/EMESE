-- Render / sima Postgres: nincs RLS és nincsenek Supabase szerepkörök
grant usage on schema nyiregyhaza to public;
grant select on all tables in schema nyiregyhaza to public;
grant execute on all functions in schema nyiregyhaza to public;
