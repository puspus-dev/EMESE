"""
EMESE API – Neon Postgres + FastAPI.

  GET /
  GET /EMESE/{city}
  GET /EMESE/{city}/stops?q=
  GET /EMESE/{city}/departures?stop=&limit=
  GET /EMESE/{city}/last-departed?stop=
  GET /EMESE/{city}/eta?trip=&date=&stop=&delay_sec=

Env:
  DATABASE_URL  – Neon connection string (sslmode=require ajánlott)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cities import load_cities

TZ = ZoneInfo("Europe/Budapest")
CITIES = load_cities()


def _normalize_database_url(raw: str) -> str:
    """Neon / Postgres URL: postgres:// → postgresql://, sslmode ha hiányzik."""
    u = raw.strip()
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://") :]
    parsed = urlparse(u)
    q = parse_qs(parsed.query)
    if "sslmode" not in q:
        q["sslmode"] = ["require"]
    # flatten single values
    flat = {k: v[0] if len(v) == 1 else v for k, v in q.items()}
    return urlunparse(parsed._replace(query=urlencode(flat)))


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="EMESE API", version="2.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def db():
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL nincs beállítva (Neon connection string)")
    return psycopg.connect(_normalize_database_url(DATABASE_URL))


def schema_of(city: str) -> str:
    c = CITIES.get(city.lower())
    if not c:
        raise HTTPException(
            404,
            detail={
                "error": f"Ismeretlen város: {city}",
                "cities": [x["key"] for x in CITIES.values()],
            },
        )
    return c["schema"]


def hhmm(iso: datetime | str) -> str:
    if isinstance(iso, str):
        iso = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return iso.astimezone(TZ).strftime("%H:%M")


def parse_at(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ).astimezone(timezone.utc)
        return d
    except ValueError as e:
        raise HTTPException(400, "Érvénytelen 'at' időpont (ISO 8601 kell)") from e


def present(row: dict[str, Any], at: datetime) -> dict[str, Any]:
    dep_dt = row["scheduled_departure"]
    if isinstance(dep_dt, str):
        dep_dt = datetime.fromisoformat(dep_dt.replace("Z", "+00:00"))
    diff = int(round((dep_dt - at).total_seconds() / 60))
    return {
        "trip_id": row["trip_id"],
        "service_date": str(row["service_date"]),
        "route": row["route"],
        "headsign": row.get("headsign"),
        "scheduled_departure": dep_dt.isoformat(),
        "scheduled_departure_local": hhmm(dep_dt),
        "minutes_until": diff,
    }


def jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def find_stops(schema: str, q: str) -> list[dict[str, str]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"select stop_id, stop_name from {schema}.find_stops(%s)", (q,))
        return [{"stop_id": a, "stop_name": b} for a, b in cur.fetchall()]


def resolve_stop(schema: str, q: str) -> tuple[str, list[str]]:
    found = find_stops(schema, q)
    if not found:
        raise HTTPException(404, f"Nincs ilyen megálló: {q}")
    exact = [
        s
        for s in found
        if s["stop_id"] == q or s["stop_name"].lower() == q.lower()
    ]
    chosen = exact or found
    names = list({s["stop_name"] for s in chosen})
    if len(names) > 1:
        raise HTTPException(
            409,
            detail={
                "error": "Több megálló is egyezik, pontosítsd a nevet",
                "candidates": names,
            },
        )
    return names[0], [s["stop_id"] for s in chosen]


def stop_events(
    schema: str, ids: list[str], at: datetime, back_min: int, fwd_min: int
) -> list[dict]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select service_date, trip_id, route, headsign, stop_id, stop_name,
                   stop_sequence, scheduled_arrival, scheduled_departure, is_last_stop
            from {schema}.stop_events(%s, %s, %s, %s)
            """,
            (ids, at, back_min, fwd_min),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/")
def root():
    return {
        "name": "EMESE API",
        "db": "neon",
        "cities": [
            {"key": c["key"], "label": c.get("label", c["key"]), "schema": c["schema"]}
            for c in CITIES.values()
        ],
        "endpoints": [
            "/EMESE/{city}/stops?q=",
            "/EMESE/{city}/departures?stop=&limit=",
            "/EMESE/{city}/last-departed?stop=",
            "/EMESE/{city}/eta?trip=&date=&stop=&delay_sec=",
        ],
    }


@app.get("/EMESE/{city}")
def city_info(city: str):
    schema = schema_of(city)
    meta = CITIES[city.lower()]
    return {
        "name": "EMESE API",
        "city": meta["key"],
        "label": meta.get("label", meta["key"]),
        "schema": schema,
        "source": "menetrend (nem valós idejű)",
        "endpoints": [
            f"/EMESE/{meta['key']}/stops?q=",
            f"/EMESE/{meta['key']}/departures?stop=&limit=",
            f"/EMESE/{meta['key']}/last-departed?stop=",
            f"/EMESE/{meta['key']}/eta?trip=&date=&stop=&delay_sec=",
        ],
    }


@app.get("/EMESE/{city}/stops")
def stops(city: str, q: str = Query(..., min_length=1)):
    schema = schema_of(city)
    return find_stops(schema, q)


@app.get("/EMESE/{city}/departures")
def departures(
    city: str,
    stop: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    at: str | None = None,
):
    schema = schema_of(city)
    at_dt = parse_at(at)
    name, ids = resolve_stop(schema, stop)
    ev = stop_events(schema, ids, at_dt, 0, 12 * 60)
    lst = []
    for e in ev:
        if e.get("is_last_stop"):
            continue
        dep = e["scheduled_departure"]
        if isinstance(dep, str):
            dep = datetime.fromisoformat(dep.replace("Z", "+00:00"))
        if dep < at_dt:
            continue
        lst.append(present(e, at_dt))
        if len(lst) >= limit:
            break
    return {
        "stop": name,
        "city": CITIES[city.lower()]["key"],
        "at": at_dt.isoformat(),
        "source": "menetrend (nem valós idejű)",
        "departures": lst,
    }


@app.get("/EMESE/{city}/last-departed")
def last_departed(
    city: str,
    stop: str = Query(...),
    lookback_min: int = Query(180, ge=5, le=720),
    at: str | None = None,
):
    schema = schema_of(city)
    at_dt = parse_at(at)
    name, ids = resolve_stop(schema, stop)
    ev = [
        e
        for e in stop_events(schema, ids, at_dt, lookback_min, 12 * 60)
        if not e.get("is_last_stop")
    ]
    past = []
    for e in ev:
        dep = e["scheduled_departure"]
        if isinstance(dep, str):
            dep = datetime.fromisoformat(dep.replace("Z", "+00:00"))
        if dep <= at_dt:
            past.append(e)
    nxt = None
    for e in ev:
        dep = e["scheduled_departure"]
        if isinstance(dep, str):
            dep = datetime.fromisoformat(dep.replace("Z", "+00:00"))
        if dep > at_dt:
            nxt = e
            break
    last = past[-1] if past else None
    return {
        "stop": name,
        "city": CITIES[city.lower()]["key"],
        "at": at_dt.isoformat(),
        "source": "menetrend (nem valós idejű)",
        "last_departed": present(last, at_dt) if last else None,
        "next_departure": present(nxt, at_dt) if nxt else None,
    }


@app.get("/EMESE/{city}/eta")
def eta(
    city: str,
    trip: str = Query(...),
    date: str = Query(...),
    stop: str = Query(...),
    delay_sec: int = Query(0, ge=-3600, le=6 * 3600),
    at: str | None = None,
):
    schema = schema_of(city)
    at_dt = parse_at(at)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select trip_id, service_date, route, headsign, status, prev_stop, next_stop,
                   progress, scheduled_arrival, eta, seconds_until
            from {schema}.trip_eta(%s, %s::date, %s, %s, %s)
            """,
            (trip, date, stop, at_dt, delay_sec),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Nincs ilyen járat")
        cols = [d.name for d in cur.description]
        data = dict(zip(cols, row))
    if not data.get("scheduled_arrival"):
        raise HTTPException(404, "A járat nem érinti ezt a megállót")
    eta_t = data["eta"]
    return {
        **{k: jsonable(v) for k, v in data.items()},
        "eta_local": hhmm(eta_t) if eta_t else None,
        "minutes_until": int(round(data["seconds_until"] / 60))
        if data.get("seconds_until") is not None
        else None,
        "source": "menetrend + megadott késés (nem valós idejű)",
    }


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
