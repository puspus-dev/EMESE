// EMESE API – Supabase Edge Function (Deno)
//   GET /api/stops?q=nyíregyháza
//   GET /api/departures?stop=Nyíregyháza, autóbusz-állomás&limit=10
//   GET /api/last-departed?stop=Nyíregyháza, autóbusz-állomás
//   GET /api/eta?trip=<trip_id>&date=2026-11-02&stop=<stop_id>&delay_sec=0
// Opcionális minden végponton: at=<ISO időpont> (teszteléshez), különben "most".
import { createClient } from "jsr:@supabase/supabase-js@2";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const TZ = "Europe/Budapest";
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

class HttpError extends Error {
  constructor(public status: number, message: string, public extra: Record<string, unknown> = {}) {
    super(message);
  }
}

type Stop = { stop_id: string; stop_name: string };
type StopEvent = {
  service_date: string;
  trip_id: string;
  route: string;
  headsign: string | null;
  stop_id: string;
  stop_name: string;
  scheduled_arrival: string;
  scheduled_departure: string;
  is_last_stop: boolean;
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
  });

const hhmm = (iso: string) =>
  new Date(iso).toLocaleTimeString("hu-HU", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });

const need = (p: URLSearchParams, key: string) => {
  const v = p.get(key)?.trim();
  if (!v) throw new HttpError(400, `Hiányzó paraméter: ${key}`);
  return v;
};

const parseAt = (p: URLSearchParams) => {
  const raw = p.get("at");
  const d = raw ? new Date(raw) : new Date();
  if (Number.isNaN(d.getTime())) throw new HttpError(400, "Érvénytelen 'at' időpont (ISO 8601 kell)");
  return d;
};

const intParam = (p: URLSearchParams, key: string, def: number, min: number, max: number) => {
  const n = Number(p.get(key) ?? def);
  if (!Number.isFinite(n)) throw new HttpError(400, `Érvénytelen szám: ${key}`);
  return Math.min(max, Math.max(min, Math.trunc(n)));
};

async function findStops(q: string): Promise<Stop[]> {
  const { data, error } = await supabase.rpc("find_stops", { p_q: q });
  if (error) throw error;
  return (data ?? []) as Stop[];
}

// Név vagy stop_id alapján megállócsoport (ugyanazon a néven több stop_id is lehet)
async function resolveStop(q: string) {
  const found = await findStops(q);
  if (!found.length) throw new HttpError(404, `Nincs ilyen megálló: ${q}`);
  const exact = found.filter((s) => s.stop_id === q || s.stop_name.toLowerCase() === q.toLowerCase());
  const chosen = exact.length ? exact : found;
  const names = [...new Set(chosen.map((s) => s.stop_name))];
  if (names.length > 1) {
    throw new HttpError(409, "Több megálló is egyezik, pontosítsd a nevet", { candidates: names });
  }
  return { name: names[0], ids: chosen.map((s) => s.stop_id) };
}

async function events(ids: string[], at: Date, backMin: number, fwdMin: number) {
  const { data, error } = await supabase.rpc("stop_events", {
    p_stop_ids: ids,
    p_at: at.toISOString(),
    p_back_min: backMin,
    p_fwd_min: fwdMin,
  });
  if (error) throw error;
  return (data ?? []) as StopEvent[];
}

const present = (e: StopEvent, at: Date) => {
  const diffMin = Math.round((new Date(e.scheduled_departure).getTime() - at.getTime()) / 60000);
  return {
    trip_id: e.trip_id,
    service_date: e.service_date,
    route: e.route,
    headsign: e.headsign,
    scheduled_departure: e.scheduled_departure,
    scheduled_departure_local: hhmm(e.scheduled_departure),
    minutes_until: diffMin, // negatív = már elindult
  };
};

async function departures(p: URLSearchParams) {
  const stop = await resolveStop(need(p, "stop"));
  const at = parseAt(p);
  const limit = intParam(p, "limit", 10, 1, 50);
  const ev = await events(stop.ids, at, 0, 12 * 60);
  const list = ev
    .filter((e) => !e.is_last_stop && new Date(e.scheduled_departure) >= at)
    .slice(0, limit)
    .map((e) => present(e, at));
  return { stop: stop.name, at: at.toISOString(), source: "menetrend (nem valós idejű)", departures: list };
}

async function lastDeparted(p: URLSearchParams) {
  const stop = await resolveStop(need(p, "stop"));
  const at = parseAt(p);
  const lookbackMin = intParam(p, "lookback_min", 180, 5, 720);
  const ev = (await events(stop.ids, at, lookbackMin, 12 * 60)).filter((e) => !e.is_last_stop);
  const past = ev.filter((e) => new Date(e.scheduled_departure) <= at);
  const next = ev.find((e) => new Date(e.scheduled_departure) > at);
  const last = past.at(-1);
  return {
    stop: stop.name,
    at: at.toISOString(),
    source: "menetrend (nem valós idejű)",
    last_departed: last ? present(last, at) : null,
    next_departure: next ? present(next, at) : null,
  };
}

async function eta(p: URLSearchParams) {
  const at = parseAt(p);
  const stopId = need(p, "stop");
  const { data, error } = await supabase.rpc("trip_eta", {
    p_trip_id: need(p, "trip"),
    p_service_date: need(p, "date"),
    p_stop_id: stopId,
    p_at: at.toISOString(),
    p_delay_sec: intParam(p, "delay_sec", 0, -3600, 6 * 3600),
  });
  if (error) throw error;
  const row = data?.[0];
  if (!row) throw new HttpError(404, "Nincs ilyen járat");
  if (!row.scheduled_arrival) throw new HttpError(404, "A járat nem érinti ezt a megállót");
  return {
    ...row,
    eta_local: hhmm(row.eta),
    minutes_until: Math.round(row.seconds_until / 60),
    source: "menetrend + megadott késés (nem valós idejű)",
  };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  try {
    const url = new URL(req.url);
    const route = url.pathname.split("/").filter(Boolean).pop() ?? "";
    const p = url.searchParams;
    switch (route) {
      case "stops":
        return json(await findStops(need(p, "q")));
      case "departures":
        return json(await departures(p));
      case "last-departed":
        return json(await lastDeparted(p));
      case "eta":
        return json(await eta(p));
      default:
        return json({
          name: "EMESE API",
          endpoints: ["/EMESE/Nyiregyhaza/stops?q=", "/EMESE/Nyiregyhaza/departures?stop=&limit=", "/EMESE/Nyiregyhaza/last-departed?stop=", "/EMESE/Nyiregyhaza/eta?trip=&date=&stop=&delay_sec="],
        });
    }
  } catch (e) {
    if (e instanceof HttpError) return json({ error: e.message, ...e.extra }, e.status);
    console.error(e);
    return json({ error: "Belső hiba" }, 500);
  }
});
