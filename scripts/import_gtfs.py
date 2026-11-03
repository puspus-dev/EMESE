#!/usr/bin/env python3
"""Volánbusz GTFS -> Postgres importer (egy feed, több város).

Minden városmappához (amelyben van city.json) letölti/betölti a városhoz tartozó járatokat:
azokat, amelyek érintenek a megadott előtagú megállót. Az adatok városonként külön sémába kerülnek.
A feed címe be van égetve a kódba. Egyetlen kötelező érték a Postgres connection string
(jelszóval), ezt nem szabad a repóba írni: DATABASE_URL (Neon connection string)
vagy --db-url.

Kapcsolók (mind opcionális):
  --city NÉV [NÉV ...]  csak ezeket a városokat (mappanév, pl. Kisvarda); kényszerített import
  --force               akkor is importál, ha a feed nem változott
  --file ZIP            helyi GTFS zip használata (nincs változásellenőrzés)
  --url URL             másik feed-cím (alapból a gtfs.kti.hu Volánbusz feed)
"""
import argparse
import csv
import hashlib
import io
import os
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import psycopg
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode


def normalize_db_url(raw: str) -> str:
    u = raw.strip()
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    parsed = urlparse(u)
    q = parse_qs(parsed.query)
    if "sslmode" not in q:
        q["sslmode"] = ["require"]
    flat = {k: v[0] if len(v) == 1 else v for k, v in q.items()}
    return urlunparse(parsed._replace(query=urlencode(flat)))


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cities import load_cities  # noqa: E402

DEFAULT_URL = "https://gtfs.kti.hu/public-gtfs/volanbusz_gtfs.zip"


def parse_args():
    ap = argparse.ArgumentParser(description="Volánbusz GTFS import")
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--city", nargs="+")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--url", default=DEFAULT_URL)
    return ap.parse_args()


ARGS = parse_args()
if not ARGS.db_url:
    sys.exit("Hiányzik a Postgres connection string: DATABASE_URL (Neon) vagy --db-url.")
ARGS.db_url = normalize_db_url(ARGS.db_url)
DB_URL = ARGS.db_url


def ensure_schema(cities):
    """Lefuttatja a városok sql/*.sql fájljait sorrendben (idempotensek), nem kell kézzel migrálni."""
    with psycopg.connect(DB_URL) as conn:
        for c in cities:
            for f in sorted((c["dir"] / "sql").glob("*.sql")):
                conn.execute(f.read_text(encoding="utf-8"))


def common_meta(cities) -> dict:
    """Az utolsó import adatai (ETag, Last-Modified, sha256) – csak akkor, ha minden város
    ugyanazt a feed-verziót tartalmazza és mindegyikben van adat. Különben üres: teljes import kell."""
    metas = []
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        for c in cities:
            cur.execute(f"select key, value from {c['schema']}.import_meta")
            metas.append(dict(cur.fetchall()))
            cur.execute(f"select exists (select 1 from {c['schema']}.stops)")
            if not cur.fetchone()[0]:
                return {}
    base = metas[0]
    return base if base.get("sha256") and all(m == base for m in metas) else {}


def open_zip(meta: dict):
    """(zip, új_meta) – vagy (None, None), ha a feed az utolsó import óta nem változott.
    Üres meta = nincs változásellenőrzés."""
    if ARGS.file:
        return zipfile.ZipFile(ARGS.file), {}
    headers = {"User-Agent": "EMESE-gtfs-import/1.0"}
    if meta.get("etag"):  # feltételes lekérés: ha a szerver támogatja, 304-et kapunk és nem töltünk le semmit
        headers["If-None-Match"] = meta["etag"]
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(urllib.request.Request(ARGS.url, headers=headers), timeout=300) as resp:
            new = {"etag": resp.headers.get("ETag") or "",
                   "last_modified": resp.headers.get("Last-Modified") or ""}
            while chunk := resp.read(1 << 20):
                tmp.write(chunk)
                digest.update(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, None
        raise
    finally:
        tmp.close()
    new["sha256"] = digest.hexdigest()
    if new["sha256"] == meta.get("sha256"):  # ha a szerver nem tud 304-et, a hash még véd
        return None, None
    return zipfile.ZipFile(tmp.name), new


def rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def sec(s):
    """'HH:MM:SS' (HH akár >= 24) -> másodperc; üres -> None."""
    s = (s or "").strip()
    if not s:
        return None
    h, m, x = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(x)


def iso_date(s: str) -> str:
    s = s.strip()
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def import_city(z, names, all_stops, city, new_meta) -> bool:
    schema, label = city["schema"], city["key"]
    selected = {sid for sid, r in all_stops.items()
                if any(r["stop_name"].lower().startswith(p.lower()) for p in city["prefixes"])}
    if not selected:
        print(f"[{label}] HIBA: nincs {city['prefixes']} előtagú megálló a feedben, kihagyva.")
        return False

    # 1. menet: mely járatok érintik a kiválasztott megállókat
    keep_trips = {r["trip_id"] for r in rows(z, "stop_times.txt") if r["stop_id"] in selected}

    # 2. menet: ezeknek a járatoknak az összes megállási ideje
    stop_times, used_stops, skipped = [], set(), 0
    for r in rows(z, "stop_times.txt"):
        if r["trip_id"] not in keep_trips:
            continue
        arr, dep = sec(r.get("arrival_time")), sec(r.get("departure_time"))
        arr = dep if arr is None else arr
        dep = arr if dep is None else dep
        if arr is None:
            skipped += 1
            continue
        stop_times.append((r["trip_id"], int(r["stop_sequence"]), r["stop_id"], arr, dep))
        used_stops.add(r["stop_id"])

    trips = [(r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign") or None,
              int(r["direction_id"]) if r.get("direction_id") not in (None, "") else None)
             for r in rows(z, "trips.txt") if r["trip_id"] in keep_trips]
    service_ids = {t[2] for t in trips}
    route_ids = {t[1] for t in trips}

    routes = [(r["route_id"], r.get("route_short_name") or None, r.get("route_long_name") or None)
              for r in rows(z, "routes.txt") if r["route_id"] in route_ids]

    stops = [(sid, all_stops[sid]["stop_name"],
              float(all_stops[sid]["stop_lat"]) if all_stops[sid].get("stop_lat") else None,
              float(all_stops[sid]["stop_lon"]) if all_stops[sid].get("stop_lon") else None)
             for sid in sorted(used_stops) if sid in all_stops]

    calendar = []
    if "calendar.txt" in names:
        calendar = [(r["service_id"], int(r["monday"]), int(r["tuesday"]), int(r["wednesday"]),
                     int(r["thursday"]), int(r["friday"]), int(r["saturday"]), int(r["sunday"]),
                     iso_date(r["start_date"]), iso_date(r["end_date"]))
                    for r in rows(z, "calendar.txt") if r["service_id"] in service_ids]

    calendar_dates = []
    if "calendar_dates.txt" in names:
        calendar_dates = [(r["service_id"], iso_date(r["date"]), int(r["exception_type"]))
                          for r in rows(z, "calendar_dates.txt") if r["service_id"] in service_ids]

    # Egy tranzakcióban cseréljük az adatot: közben az olvasók a régit látják
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(f"truncate {schema}.stops, {schema}.routes, {schema}.trips, {schema}.stop_times, "
                    f"{schema}.calendar, {schema}.calendar_dates")
        for k, v in new_meta.items():
            cur.execute(f"insert into {schema}.import_meta (key, value) values (%s, %s) "
                        "on conflict (key) do update set value = excluded.value", (k, v))

        def copy(table, cols, data):
            with cur.copy(f"copy {schema}.{table} ({cols}) from stdin") as cp:
                for row in data:
                    cp.write_row(row)

        copy("stops", "stop_id, stop_name, stop_lat, stop_lon", stops)
        copy("routes", "route_id, route_short_name, route_long_name", routes)
        copy("trips", "trip_id, route_id, service_id, trip_headsign, direction_id", trips)
        copy("stop_times", "trip_id, stop_sequence, stop_id, arrival_sec, departure_sec", stop_times)
        copy("calendar", "service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday, "
                         "start_date, end_date", calendar)
        copy("calendar_dates", "service_id, date, exception_type", calendar_dates)

    print(f"[{label}] OK: {len(stops)} megálló, {len(routes)} viszonylat, {len(trips)} járat, "
          f"{len(stop_times)} megállási idő ({skipped} idő nélküli kihagyva), "
          f"{len(calendar)} naptár, {len(calendar_dates)} kivétel.")
    return True


def main():
    cities = load_cities()
    if ARGS.city:
        unknown = [k for k in ARGS.city if k.lower() not in cities]
        if unknown:
            sys.exit(f"Ismeretlen város: {unknown}. Elérhető: {[c['key'] for c in cities.values()]}")
        todo = [cities[k.lower()] for k in ARGS.city]
    else:
        todo = list(cities.values())

    ensure_schema(todo)
    # --city / --force: kényszerített import (nincs változásellenőrzés)
    meta = {} if (ARGS.city or ARGS.force) else common_meta(todo)
    z, new_meta = open_zip(meta)
    if z is None:
        print("A feed nem változott az utolsó import óta, nincs teendő.")
        return
    names = set(z.namelist())
    all_stops = {r["stop_id"]: r for r in rows(z, "stops.txt")}

    failed = [c["key"] for c in todo if not import_city(z, names, all_stops, c, new_meta)]
    if failed:
        sys.exit(f"Sikertelen: {failed}")


if __name__ == "__main__":
    main()
