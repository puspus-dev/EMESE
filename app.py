"""
EMESE API – Render / sima Postgres változat. Egy service, több város.

  GET /                                  városok és végpontok listája
  GET /EMESE/{city}                      a város adatai
  GET /EMESE/{city}/stops?q=
  GET /EMESE/{city}/departures?stop=&limit=
  GET /EMESE/{city}/last-departed?stop=
  GET /EMESE/{city}/eta?trip=&date=&stop=&delay_sec=

A {city} a városmappa neve (pl. Kisvarda, Nyiregyhaza; kis/nagybetű mindegy). Új város:
új mappa city.json-nal és sql/ fájlokkal, az app és az importer magától felfedezi.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cities import load_cities

TZ = ZoneInfo("Europe/Budapest")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or ""
CITIES = load_cities()

app = FastAPI(title="EMESE API", version="2.0.0")
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


def schema_of(city: str) -> str:
    c = CITIES.get(city.lower())
    if not c:
        raise HTTPException(404, detail={"error": f"Ismeretlen város: {city}",
                                         "cities": [x["key"] for x in CITIES.values()]})
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
    diff = int(round((dep_dt - at).total_seconds() / 60))
    return {
        "trip_id": row["trip_id"],
        "service_date": str(row["service_date"]),
        "route": row["route"],
        "headsign": row.get("headsign"),
        "scheduled_departure": dep_dt.isoformat(),
        "scheduled_departure_local": hhmm(dep_dt),
        "minutes_until": diff,  # negatív = már elindult
    }


def find_stops(schema: str, q: str) -> list[dict[str, str]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"select stop_id, stop_name from {schema}.find_stops(%s)", (q,))
        return [{"stop_id": a, "stop_name": b} for a, b in cur.fetchall()]


def resolve_stop(schema: str, q: str) -> tuple[str, list[str]]:
    found = find_stops(schema, q)
    if not found:
        raise HTTPException(404, f"Nincs ilyen megálló: {q}")
    exact = [s for s in found if s["stop_id"] == q or s["stop_name"].lower() == q.lower()]
    chosen = exact or found
    names = sorted({s["stop_name"] for s in chosen})
    if len(names) > 1:
        raise HTTPException(
            409,
            detail={"error": "Több megálló is egyezik, pontosítsd a nevet", "candidates": names},
        )
    return names[0], [s["stop_id"] for s in chosen]


def stop_events(schema: str, ids: list[str], at: datetime, back_min: int, fwd_min: int) -> list[dict]:
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
        "source": "menetrend (nem valós idejű)",
        "cities": [{"city": c["key"], "name": c["name"], "base": f"/EMESE/{c['key']}"} for c in CITIES.values()],
        "endpoints": ["/EMESE/{city}/stops?q=", "/EMESE/{city}/departures?stop=&limit=",
                      "/EMESE/{city}/last-departed?stop=", "/EMESE/{city}/eta?trip=&date=&stop=&delay_sec="],
    }


@app.get("/EMESE/{city}")
def city_info(city: str):
    schema_of(city)
    c = CITIES[city.lower()]
    base = f"/EMESE/{c['key']}"
    return {
        "city": c["key"],
        "name": c["name"],
        "source": "menetrend (nem valós idejű)",
        "endpoints": [f"{base}/stops?q=", f"{base}/departures?stop=&limit=",
                      f"{base}/last-departed?stop=", f"{base}/eta?trip=&date=&stop=&delay_sec="],
    }


@app.get("/EMESE/{city}/stops")
def stops(city: str, q: str = Query(..., min_length=1)):
    return find_stops(schema_of(city), q)


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
    ev = [e for e in stop_events(schema, ids, at_dt, lookback_min, 12 * 60) if not e.get("is_last_stop")]
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
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if isinstance(v, Decimal) else v)
           for k, v in data.items()}
    out["eta_local"] = hhmm(eta_t) if eta_t else None
    out["minutes_until"] = int(round(data["seconds_until"] / 60)) if data.get("seconds_until") is not None else None
    out["source"] = "menetrend + megadott késés (nem valós idejű)"
    return out


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
