-- Neon Postgres: nincs RLS és nincsenek extra szerepkörök
grant usage on schema kisvarda to public;
grant select on all tables in schema kisvarda to public;
grant execute on all functions in schema kisvarda to public;
