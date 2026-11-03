-- A(z) Kisvárda város adatai a(z) kisvarda sémában vannak (egy Postgres, városonként külön séma).
create schema if not exists kisvarda;

-- EMESE: Volánbusz GTFS séma (a Kisvárda környékére szűrt adathalmazhoz)
-- Az időket a service day 00:00-jától számolt másodpercben tároljuk, mert a GTFS
-- 24:00 utáni időket is használ (pl. 25:10:00 = másnap 01:10).

create table if not exists kisvarda.stops (
  stop_id   text primary key,
  stop_name text not null,
  stop_lat  double precision,
  stop_lon  double precision
);

create table if not exists kisvarda.routes (
  route_id         text primary key,
  route_short_name text,
  route_long_name  text
);

create table if not exists kisvarda.trips (
  trip_id       text primary key,
  route_id      text not null,
  service_id    text not null,
  trip_headsign text,
  direction_id  smallint
);

create table if not exists kisvarda.stop_times (
  trip_id       text    not null,
  stop_sequence integer not null,
  stop_id       text    not null,
  arrival_sec   integer not null,
  departure_sec integer not null,
  primary key (trip_id, stop_sequence)
);
create index if not exists stop_times_stop_idx on kisvarda.stop_times (stop_id, departure_sec);
create index if not exists trips_service_idx   on kisvarda.trips (service_id);

create table if not exists kisvarda.calendar (
  service_id text primary key,
  monday smallint not null, tuesday smallint not null, wednesday smallint not null,
  thursday smallint not null, friday smallint not null, saturday smallint not null,
  sunday smallint not null,
  start_date date not null,
  end_date   date not null
);

create table if not exists kisvarda.calendar_dates (
  service_id     text     not null,
  date           date     not null,
  exception_type smallint not null,  -- 1 = hozzáadva, 2 = kivéve
  primary key (service_id, date)
);
