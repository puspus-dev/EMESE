# EMESE API (Neon Postgres)

Volánbusz menetrend alapú indulás/érkezés API. **Csak Neon Postgres** – nincs Supabase, nincs Render DB.

## Stack

| Réteg | Technológia |
|--------|-------------|
| Adatbázis | [Neon](https://neon.tech) (serverless Postgres) |
| API | FastAPI + psycopg (`app.py`) |
| Import | `scripts/import_gtfs.py` |

## Végpontok

```
GET /
GET /EMESE/{city}
GET /EMESE/{city}/stops?q=
GET /EMESE/{city}/departures?stop=&limit=
GET /EMESE/{city}/last-departed?stop=
GET /EMESE/{city}/eta?trip=&date=&stop=&delay_sec=
```

`{city}`: `Kisvarda` | `Nyiregyhaza` (kis/nagybetű mindegy)

## 1. Neon projekt

1. [console.neon.tech](https://console.neon.tech) → New Project  
2. **Connection string** másolása (URI), pl.:

```text
postgresql://user:pass@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require
```

3. Env:

```bash
export DATABASE_URL='postgresql://…?sslmode=require'
```

## 2. Séma (SQL)

Neon **SQL Editor**ben, városenként sorban:

```text
Nyiregyhaza/sql/0001_gtfs_schema.sql
Nyiregyhaza/sql/0002_functions.sql
Nyiregyhaza/sql/0003_import_meta.sql
Nyiregyhaza/sql/0004_neon_grants.sql
```

Ugyanígy `Kisvarda/sql/…`.

Vagy CLI:

```bash
psql "$DATABASE_URL" -f Nyiregyhaza/sql/0001_gtfs_schema.sql
psql "$DATABASE_URL" -f Nyiregyhaza/sql/0002_functions.sql
psql "$DATABASE_URL" -f Nyiregyhaza/sql/0003_import_meta.sql
psql "$DATABASE_URL" -f Nyiregyhaza/sql/0004_neon_grants.sql
# + Kisvarda
```

## 3. GTFS import

```bash
pip install 'psycopg[binary]'
python scripts/import_gtfs.py --prefix "Nyíregyháza"
python scripts/import_gtfs.py --prefix "Kisvárda"
# vagy a script több-város módja, ha a cities.py / mappák szerint van
```

## 4. API indítás

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql://…?sslmode=require'
uvicorn app:app --host 0.0.0.0 --port 8000
```

Teszt:

```text
http://localhost:8000/
http://localhost:8000/EMESE/Nyiregyhaza/stops?q=ny
```

Az API-t futtathatod bárhol (Vercel-szerű Python host, Fly.io, saját VPS, Railway **csak a web process**, DB = Neon).

## Env

| Változó | Kötelező | Leírás |
|---------|----------|--------|
| `DATABASE_URL` | igen | Neon connection string |

Nincs `SUPABASE_*`, nincs Render-specifikus változó.

## Új város

Új mappa `city.json` + `sql/` fájlokkal; `cities.py` automatikusan betölti.
