-- EMESE: lekérdező függvények. Minden időt Europe/Budapest szerint értelmezünk.

-- Mely service_id-k aktívak az adott napon (calendar + calendar_dates kivételek)
create or replace function nyiregyhaza.active_services(p_date date)
returns table (service_id text)
language sql stable as $$
  select c.service_id
  from nyiregyhaza.calendar c
  where p_date between c.start_date and c.end_date
    and (case extract(isodow from p_date)::int
           when 1 then c.monday   when 2 then c.tuesday  when 3 then c.wednesday
           when 4 then c.thursday when 5 then c.friday   when 6 then c.saturday
           else c.sunday end) = 1
    and not exists (select 1 from nyiregyhaza.calendar_dates x
                    where x.service_id = c.service_id and x.date = p_date and x.exception_type = 2)
  union
  select x.service_id
  from nyiregyhaza.calendar_dates x
  where x.date = p_date and x.exception_type = 1
$$;

-- Megálló keresése azonosító vagy névrészlet alapján
create or replace function nyiregyhaza.find_stops(p_q text)
returns table (stop_id text, stop_name text)
language sql stable as $$
  select s.stop_id, s.stop_name
  from nyiregyhaza.stops s
  where s.stop_id = p_q or s.stop_name ilike '%' || p_q || '%'
  order by (lower(s.stop_name) = lower(p_q)) desc, s.stop_name, s.stop_id
  limit 50
$$;

-- Egy megálló(k) menetrend szerinti eseményei a [p_at - back, p_at + fwd] ablakban.
-- A tegnapi/mai/holnapi service day-t is vizsgálja (éjfél utáni járatok miatt).
create or replace function nyiregyhaza.stop_events(
  p_stop_ids text[],
  p_at       timestamptz default now(),
  p_back_min integer     default 120,
  p_fwd_min  integer     default 180
)
returns table (
  service_date date, trip_id text, route text, headsign text,
  stop_id text, stop_name text, stop_sequence integer,
  scheduled_arrival timestamptz, scheduled_departure timestamptz,
  is_last_stop boolean
)
language sql stable as $$
  with local_day as (
    select (p_at at time zone 'Europe/Budapest')::date as d
  ),
  days as (
    select g::date as service_date
    from local_day, generate_series((d - 1)::timestamp, (d + 1)::timestamp, interval '1 day') g
  ),
  ev as (
    select dd.service_date,
           t.trip_id,
           coalesce(nullif(r.route_short_name, ''), r.route_long_name) as route,
           t.trip_headsign as headsign,
           st.stop_id,
           s.stop_name,
           st.stop_sequence,
           (dd.service_date::timestamp + make_interval(secs => st.arrival_sec))   at time zone 'Europe/Budapest' as scheduled_arrival,
           (dd.service_date::timestamp + make_interval(secs => st.departure_sec)) at time zone 'Europe/Budapest' as scheduled_departure,
           not exists (select 1 from nyiregyhaza.stop_times x
                       where x.trip_id = st.trip_id and x.stop_sequence > st.stop_sequence) as is_last_stop
    from days dd
    cross join lateral nyiregyhaza.active_services(dd.service_date) a
    join nyiregyhaza.trips t       on t.service_id = a.service_id
    join nyiregyhaza.routes r      on r.route_id   = t.route_id
    join nyiregyhaza.stop_times st on st.trip_id   = t.trip_id
    join nyiregyhaza.stops s       on s.stop_id    = st.stop_id
    where st.stop_id = any (p_stop_ids)
  )
  select * from ev
  where scheduled_departure between p_at - make_interval(mins => p_back_min)
                                and p_at + make_interval(mins => p_fwd_min)
  order by scheduled_departure
$$;

-- Egy járat becsült érkezése egy megállóba + hol tart most.
-- p_delay_sec: opcionális késés (pozitív = késik); a GTFS-ben nincs valós idejű adat.
create or replace function nyiregyhaza.trip_eta(
  p_trip_id      text,
  p_service_date date,
  p_stop_id      text,
  p_at           timestamptz default now(),
  p_delay_sec    integer     default 0
)
returns table (
  trip_id text, service_date date, route text, headsign text, status text,
  prev_stop text, next_stop text, progress numeric,
  scheduled_arrival timestamptz, eta timestamptz, seconds_until integer
)
language sql stable as $$
  with tt as (
    select st.stop_sequence, st.stop_id, s.stop_name,
           (p_service_date::timestamp + make_interval(secs => st.arrival_sec))   at time zone 'Europe/Budapest' as arr,
           (p_service_date::timestamp + make_interval(secs => st.departure_sec)) at time zone 'Europe/Budapest' as dep
    from nyiregyhaza.stop_times st
    join nyiregyhaza.stops s on s.stop_id = st.stop_id
    where st.trip_id = p_trip_id
  ),
  eff as (select p_at - make_interval(secs => p_delay_sec) as ts),
  tgt as (select arr from tt where stop_id = p_stop_id order by stop_sequence limit 1),
  prv as (select tt.stop_name, tt.dep from tt, eff where tt.dep <= eff.ts order by tt.stop_sequence desc limit 1),
  nxt as (select tt.stop_name, tt.arr from tt, eff where tt.arr >  eff.ts order by tt.stop_sequence asc  limit 1)
  select t.trip_id,
         p_service_date,
         coalesce(nullif(r.route_short_name, ''), r.route_long_name),
         t.trip_headsign,
         case when prv.stop_name is null then 'not_started'
              when nxt.stop_name is null then 'finished'
              else 'in_transit' end,
         prv.stop_name,
         nxt.stop_name,
         case when prv.stop_name is not null and nxt.stop_name is not null
              then round(least(1, greatest(0,
                     extract(epoch from (eff.ts - prv.dep))
                     / nullif(extract(epoch from (nxt.arr - prv.dep)), 0)))::numeric, 3) end,
         tgt.arr,
         tgt.arr + make_interval(secs => p_delay_sec),
         extract(epoch from (tgt.arr + make_interval(secs => p_delay_sec) - p_at))::integer
  from nyiregyhaza.trips t
  join nyiregyhaza.routes r on r.route_id = t.route_id
  cross join eff
  left join prv on true
  left join nxt on true
  left join tgt on true
  where t.trip_id = p_trip_id
$$;
