"""Városok felderítése: minden gyökérmappa város, amelyben van city.json.

city.json: {"name": "Kisvárda", "schema": "kisvarda", "prefix": "Kisvárda"}
  schema  – a város Postgres-sémája (kisbetű, szám, aláhúzás)
  prefix  – megállónév-előtag(ok), ';'-vel elválasztva: az importer az ilyen nevű megállókat érintő járatokat tölti be
A mappa neve (pl. Kisvarda) az URL-ben használt város-azonosító (kis/nagybetű mindegy).
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_cities(root: Path = ROOT) -> dict[str, dict]:
    cities: dict[str, dict] = {}
    for d in sorted(root.iterdir()):
        cfg = d / "city.json"
        if not (d.is_dir() and cfg.is_file()):
            continue
        c = json.loads(cfg.read_text(encoding="utf-8"))
        schema = c["schema"]
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError(f"{cfg}: érvénytelen séma-név: {schema!r}")
        cities[d.name.lower()] = {
            "key": d.name,
            "dir": d,
            "name": c.get("name", d.name),
            "schema": schema,
            "prefixes": [p.strip() for p in c["prefix"].split(";") if p.strip()],
        }
    if not cities:
        raise RuntimeError("Nincs egyetlen város sem (city.json) a repóban.")
    return cities
