-- Neon Postgres: nincs RLS és nincsenek extra szerepkörök
grant usage on schema nyiregyhaza to public;
grant select on all tables in schema nyiregyhaza to public;
grant execute on all functions in schema nyiregyhaza to public;
