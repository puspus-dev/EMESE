"""
EMESE API – Render / sima Postgres változat
Ugyanazok a végpontok, mint a puspus-dev/EMESE Supabase Edge Function.

Path (város prefix opcionális, egy Render service = egy város DB):
  GET /
  GET /stops?q=
  GET /departures?stop=&limit=
  GET /last-departed?stop=
  GET /eta?trip=&date=&stop=&delay_sec=

Kompatibilis path-ok:
  /EMESE/{city}/stops ...
  /api/stops ...
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

TZ = ZoneInfo("Europe/Budapest")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or ""
CITY = os.environ.get("EMESE_CITY", "Nyiregyhaza")
SCHEMA = CITY.lower()  # a város Postgres-sémája (az importer hozza létre a sql/ fájlokból)
if not re.fullmatch(r"[a-z_][a-z0-9_]*", SCHEMA):
    raise RuntimeError(f"Érvénytelen EMESE_CITY: {CITY}")

app = FastAPI(title="EMESE API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def db():
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL nincs beállítva")
    # Render néha postgres:// scheme-et ad
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


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
    dep = row["scheduled_departure"]
    if isinstance(dep, str):
        dep_dt = datetime.fromisoformat(dep.replace("Z", "+00:00"))
    else:
        dep_dt = dep
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


def find_stops(q: str) -> list[dict[str, str]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"select stop_id, stop_name from {SCHEMA}.find_stops(%s)", (q,))
        return [{"stop_id": a, "stop_name": b} for a, b in cur.fetchall()]


def resolve_stop(q: str) -> tuple[str, list[str]]:
    found = find_stops(q)
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
            detail={"error": "Több megálló is egyezik, pontosítsd a nevet", "candidates": names},
        )
    return names[0], [s["stop_id"] for s in chosen]


def stop_events(ids: list[str], at: datetime, back_min: int, fwd_min: int) -> list[dict]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select service_date, trip_id, route, headsign, stop_id, stop_name,
                   stop_sequence, scheduled_arrival, scheduled_departure, is_last_stop
            from {SCHEMA}.stop_events(%s, %s, %s, %s)
            """,
            (ids, at, back_min, fwd_min),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/")
@app.get(f"/EMESE/{CITY}")
@app.get("/EMESE/{city}")
def root(city: str | None = None):
    return {
        "name": "EMESE API",
        "city": city or CITY,
        "source": "menetrend (nem valós idejű)",
        "endpoints": [
            f"/EMESE/{CITY}/stops?q=",
            f"/EMESE/{CITY}/departures?stop=&limit=",
            f"/EMESE/{CITY}/last-departed?stop=",
            f"/EMESE/{CITY}/eta?trip=&date=&stop=&delay_sec=",
            "/stops?q=",
            "/departures?stop=&limit=",
        ],
    }


@app.get("/stops")
@app.get("/api/stops")
@app.get(f"/EMESE/{CITY}/stops")
@app.get("/EMESE/{city}/stops")
def stops(q: str = Query(..., min_length=1), city: str | None = None):
    return find_stops(q)


@app.get("/departures")
@app.get("/api/departures")
@app.get(f"/EMESE/{CITY}/departures")
@app.get("/EMESE/{city}/departures")
def departures(
    stop: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    at: str | None = None,
    city: str | None = None,
):
    at_dt = parse_at(at)
    name, ids = resolve_stop(stop)
    ev = stop_events(ids, at_dt, 0, 12 * 60)
    lst = [
        present(e, at_dt)
        for e in ev
        if not e.get("is_last_stop") and e["scheduled_departure"] >= at_dt
    ][:limit]
    return {
        "stop": name,
        "at": at_dt.isoformat(),
        "source": "menetrend (nem valós idejű)",
        "departures": lst,
    }


@app.get("/last-departed")
@app.get("/api/last-departed")
@app.get(f"/EMESE/{CITY}/last-departed")
@app.get("/EMESE/{city}/last-departed")
def last_departed(
    stop: str = Query(...),
    lookback_min: int = Query(180, ge=5, le=720),
    at: str | None = None,
    city: str | None = None,
):
    at_dt = parse_at(at)
    name, ids = resolve_stop(stop)
    ev = [e for e in stop_events(ids, at_dt, lookback_min, 12 * 60) if not e.get("is_last_stop")]
    past = [e for e in ev if e["scheduled_departure"] <= at_dt]
    nxt = next((e for e in ev if e["scheduled_departure"] > at_dt), None)
    last = past[-1] if past else None
    return {
        "stop": name,
        "at": at_dt.isoformat(),
        "source": "menetrend (nem valós idejű)",
        "last_departed": present(last, at_dt) if last else None,
        "next_departure": present(nxt, at_dt) if nxt else None,
    }


@app.get("/eta")
@app.get("/api/eta")
@app.get(f"/EMESE/{CITY}/eta")
@app.get("/EMESE/{city}/eta")
def eta(
    trip: str = Query(...),
    date: str = Query(...),
    stop: str = Query(...),
    delay_sec: int = Query(0, ge=-3600, le=6 * 3600),
    at: str | None = None,
    city: str | None = None,
):
    at_dt = parse_at(at)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select trip_id, service_date, route, headsign, status, prev_stop, next_stop,
                   progress, scheduled_arrival, eta, seconds_until
            from {SCHEMA}.trip_eta(%s, %s::date, %s, %s, %s)
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
        **{k: (str(v) if hasattr(v, "isoformat") else v) for k, v in data.items()},
        "eta_local": hhmm(eta_t) if eta_t else None,
        "minutes_until": int(round(data["seconds_until"] / 60)) if data.get("seconds_until") is not None else None,
        "source": "menetrend + megadott késés (nem valós idejű)",
    }


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
