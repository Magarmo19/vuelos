#!/usr/bin/env python3
"""
Rastreador de la flota de VistaJet (o cualquier operador) con ADS-B públicos.

En cada consulta pide a las APIs todos los aviones de los tipos configurados
(Global 7500, Challenger 350...) y se queda con los que vuelan con el indicativo
del operador (VJT...). Aprende la matrícula y el código hex de cada avión que ve,
así que la flota se va completando sola.

Para cada avión detecta despegues y aterrizajes y genera, en DATA_DIR:
  flota.json             estado en directo de toda la flota (lo lee la web)
  historial_flota.json   vuelos cerrados de los últimos N días (sin trayectoria)
  ella.json              vuelos de la persona según asignaciones.json (con trayectoria)
  vuelos.ics             calendario solo con sus vuelos
  state.json             estado interno (incluye trayectorias de vuelos en curso)
  _tracks.json           trayectorias de los vuelos cerrados recientes (interno)

Solo librería estándar de Python 3.9+.
"""
import gzip
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
CONFIG = os.environ.get("CONFIG_FILE", os.path.join(ROOT, "config.json"))
ASSIGN = os.environ.get("ASSIGN_FILE", os.path.join(ROOT, "asignaciones.json"))
AIRPORTS_FILE = os.path.join(ROOT, "tracker", "airports.json")

MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "250"))  # duración de una ejecución
POLL_EVERY = int(os.environ.get("POLL_EVERY", "60"))     # segundos entre consultas
PUSH_EVERY = int(os.environ.get("PUSH_EVERY", "120"))    # cada cuánto publicar datos a mitad de ejecución
PUSH_CMD = os.environ.get("PUSH_CMD")                    # comando para publicar (lo pone el workflow)
REQ_PAUSE = float(os.environ.get("REQ_PAUSE", "1.1"))    # pausa entre peticiones (límite 1 req/s)

TZ = ZoneInfo("Europe/Madrid")
AIRBORNE_MIN_GS = 70      # nudos
LANDED_LOST_MIN = 15      # min sin señal estando bajo => aterrizado
LOW_ALT_FT = 5000
LOST_CLOSE_HOURS = 8      # sin señal en crucero tanto tiempo => vuelo "sin señal"
FILL_GAP_HOURS = 12       # completar destino si reaparece en tierra antes de esto
LIVE_TRACK_POINTS = 150   # puntos de trayectoria por vuelo en flota.json
STORE_TRACK_POINTS = 400  # puntos guardados por vuelo cerrado
TRACE_URL = "https://adsb.lol/data/traces/{k}/{f}_{h}.json"  # recorrido del día de cada avión (adsb.lol)
TRACE_RETRY_MIN = 15      # si no sale el origen, reintentar con el recorrido cada tanto
GAP_MIN = 30              # minutos sin puntos a partir de los cuales hay un "hueco" en el vuelo
CRUISE_KT = 430           # velocidad de crucero típica (nudos) para estimar llegadas tras un hueco
SAME_FLIGHT_MIN_KT = 250  # tras un hueco, si la velocidad media implícita es menor, aterrizó en medio
MAX_FLIGHT_MIN = 20 * 60  # ningún avión de la flota vuela más de esto seguido
REOPEN_MAX_HOURS = 6      # un vuelo "sin señal" solo se reabre si vuelve a verse antes de esto

API_TYPE = [
    "https://api.adsb.lol/v2/type/{q}",
    "https://api.airplanes.live/v2/type/{q}",
    "https://opendata.adsb.fi/api/v2/type/{q}",
]
API_HEX = [
    "https://api.adsb.lol/v2/hex/{q}",
    "https://api.airplanes.live/v2/hex/{q}",
    "https://opendata.adsb.fi/api/v2/hex/{q}",
]

MODELOS = {
    "GL7T": "Global 7500", "GL8T": "Global 8000", "GLEX": "Global 6000",
    "GL5T": "Global 5000", "CL60": "Challenger 605", "CL35": "Challenger 350",
    "CRJ2": "Challenger 850",
}


# ------------------------------------------------------------------ utilidades

def now_ts():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def local_date(ts):
    return datetime.fromtimestamp(ts, TZ).date()


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def haversine_km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _d(d):
    return 99999 if d is None else d


_AIRPORTS = None


def nearest_airport(lat, lon):
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = load(AIRPORTS_FILE, [])
    best, best_d = None, None
    for a in _AIRPORTS:
        if abs(a[5] - lat) > 3 or abs(a[6] - lon) > 4:
            continue
        d = haversine_km(lat, lon, a[5], a[6])
        if best_d is None or d < best_d:
            best, best_d = a, d
    if best is None:
        return None, None
    return {"icao": best[0], "iata": best[1], "nombre": best[2], "ciudad": best[3],
            "pais": best[4], "lat": best[5], "lon": best[6]}, round(best_d, 1)


def thin(track, n):
    if len(track) <= n:
        return track
    step = (len(track) - 1) / (n - 1)
    return [track[round(i * step)] for i in range(n)]


# ------------------------------------------------------------------ ADS-B

def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "vuelos-tracker/2.0 (uso personal, no comercial)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_trace(h):
    """Recorrido del día del avión según adsb.lol: [(ts, lat, lon, alt|'ground', flags), ...] ordenado."""
    pts = {}
    for f in ("trace_full", "trace_recent"):
        url = TRACE_URL.format(k=h[-2:], f=f, h=h)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "vuelos-tracker/2.0 (uso personal, no comercial)", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            d = json.loads(raw.decode("utf-8"))
            base = d["timestamp"]
            for p in d.get("trace") or []:
                ts = base + p[0]
                pts[round(ts)] = (ts, p[1], p[2], p[3], p[6] if len(p) > 6 and isinstance(p[6], int) else 0)
        except Exception as e:
            print(f"  · traza {h} {f}: {e}", file=sys.stderr)
        time.sleep(REQ_PAUSE)
    return [pts[k] for k in sorted(pts)]


def takeoff_from_trace(pts, first_ts):
    """Busca hacia atrás desde el primer punto del vuelo actual dónde despegó.
    Devuelve (aeropuerto, aproximado, hora_despegue) o None."""
    pts = [p for p in pts if p[0] <= first_ts + 120]
    if not pts or pts[-1][3] == "ground":
        return None
    i = len(pts) - 1
    while i > 0:
        if pts[i][4] & 2:                      # readsb marca aquí el inicio de un tramo nuevo
            break
        prev = pts[i - 1]
        if pts[i][0] - prev[0] > 3 * 3600:     # hueco grande: el despegue no está en el recorrido
            break
        if prev[3] == "ground":                # último punto en tierra antes de despegar
            ap, d = nearest_airport(prev[1], prev[2])
            return (ap, False, pts[i][0]) if ap and _d(d) < 15 else None
        i -= 1
    ts, lat, lon, alt, _ = pts[i]
    if isinstance(alt, (int, float)) and alt < 8000:
        ap, d = nearest_airport(lat, lon)
        if ap and _d(d) < 40:
            return ap, True, ts
    return None


def query(templates, q):
    """Prueba cada fuente hasta que una responda. Devuelve (lista_ac, now_ms) o (None, None)."""
    for tpl in templates:
        url = tpl.format(q=q)
        try:
            data = fetch_json(url)
            return data.get("ac") or [], data.get("now") or now_ts() * 1000
        except Exception as e:
            print(f"  · {url.split('/')[2]} {q}: {e}", file=sys.stderr)
            time.sleep(REQ_PAUSE)
    return None, None


def poll(cfg, state):
    """Una pasada. Devuelve (lecturas_por_hex, hexes_con_respuesta)."""
    op = cfg.get("operador", {})
    prefixes = tuple(p.upper() for p in op.get("prefijos_callsign", []))
    tipos = op.get("tipos", [])
    reg = state.setdefault("registro", {})
    extra = {p["hex"].lower(): p for p in cfg.get("siempre_rastrear", []) if p.get("hex")}
    for a in load(ASSIGN, {}).get("asignaciones", []):
        if a.get("hex"):
            extra.setdefault(a["hex"].lower(), {"matricula": a.get("matricula"), "hex": a["hex"].lower()})

    readings, answered_types = {}, set()

    def consider(ac, now_ms):
        h = (ac.get("hex") or "").lower()
        if not h or ac.get("lat") is None or ac.get("lon") is None:
            return
        cs = (ac.get("flight") or "").strip().upper()
        mine = (prefixes and cs.startswith(prefixes)) or h in reg or h in extra
        if not mine:
            return
        ac["_ts"] = now_ms / 1000 - (ac.get("seen_pos", ac.get("seen", 0)) or 0)
        if h not in readings or ac["_ts"] > readings[h]["_ts"]:
            readings[h] = ac

    for t in tipos:
        acs, now_ms = query(API_TYPE, t)
        if acs is not None:
            answered_types.add(t)
            for ac in acs:
                consider(ac, now_ms)
        time.sleep(REQ_PAUSE)

    # aviones fijos o asignados que no son de esos tipos (o que no han salido)
    answered_hex = set()
    for h in extra:
        t = (reg.get(h) or {}).get("tipo") or extra[h].get("tipo")
        if h in readings or (t in answered_types and t in tipos):
            continue
        acs, now_ms = query(API_HEX, h)
        if acs is not None:
            answered_hex.add(h)
            for ac in acs:
                consider(ac, now_ms)
        time.sleep(REQ_PAUSE)

    # actualizar el registro de la flota
    for h, ac in readings.items():
        r = reg.setdefault(h, {"desde": iso(now_ts())})
        if ac.get("r"):
            r["matricula"] = ac["r"]
        elif extra.get(h, {}).get("matricula"):
            r["matricula"] = extra[h]["matricula"]
        if ac.get("t"):
            r["tipo"] = ac["t"]
        if ac.get("desc"):
            r["desc"] = ac["desc"]
        cs = (ac.get("flight") or "").strip()
        if cs:
            r["callsign"] = cs
    for h, p in extra.items():
        r = reg.setdefault(h, {"desde": iso(now_ts())})
        r.setdefault("matricula", p.get("matricula"))
        if p.get("tipo"):
            r.setdefault("tipo", p["tipo"])

    answered = set(answered_hex)
    for h, r in reg.items():
        if r.get("tipo") in answered_types or (r.get("tipo") not in tipos and answered_types):
            answered.add(h)
    return readings, answered


def reading_from(ac):
    alt = ac.get("alt_baro")
    ground = alt == "ground"
    alt_ft = 0 if ground else (alt if isinstance(alt, (int, float)) else ac.get("alt_geom") or 0)
    gs = ac.get("gs") or 0
    return {
        "ts": round(ac["_ts"]), "lat": round(ac["lat"], 4), "lon": round(ac["lon"], 4),
        "alt": int(alt_ft), "gs": int(gs), "trk": int(ac.get("track") or ac.get("true_heading") or 0),
        "vs": int(ac.get("baro_rate") or ac.get("geom_rate") or 0),
        "callsign": (ac.get("flight") or "").strip(),
        "airborne": (not ground) and gs >= AIRBORNE_MIN_GS,
    }


# ------------------------------------------------------------------ huecos de datos

def same_flight(lp, rd):
    """¿La nueva lectura en el aire puede ser el mismo vuelo que el último punto lp=[lat,lon,alt,ts]?
    Tras un hueco largo, si el avión no pudo seguir volando todo ese tiempo (velocidad media
    implícita baja, o venía bajando), es que aterrizó y volvió a despegar sin que lo viéramos."""
    gap = rd["ts"] - lp[3]
    if gap < GAP_MIN * 60:
        return True
    if lp[2] < LOW_ALT_FT:
        return False
    kt = haversine_km(lp[0], lp[1], rd["lat"], rd["lon"]) / 1.852 / (gap / 3600)
    return kt >= SAME_FLIGHT_MIN_KT


def est_arrival(lp, lat, lon, cap_ts):
    """Hora estimada de llegada desde el último punto visto hasta (lat, lon) a velocidad de crucero."""
    km = haversine_km(lp[0], lp[1], lat, lon)
    eta = lp[3] + km / (CRUISE_KT * 1.852) * 3600 + 10 * 60
    return min(max(eta, lp[3] + 60), cap_ts)


# ------------------------------------------------------------------ vuelos

class Store:
    def __init__(self):
        self.state = load(os.path.join(DATA, "state.json"), {})
        self.state.setdefault("aviones", {})
        self.state.setdefault("registro", {})
        self.hist = load(os.path.join(DATA, "historial_flota.json"), [])
        self.tracks = load(os.path.join(DATA, "_tracks.json"), {})
        self.by_id = {f["id"]: f for f in self.hist}

    def ident(self, h):
        r = self.state["registro"].get(h, {})
        t = r.get("tipo") or ""
        return {"hex": h, "matricula": r.get("matricula") or h.upper(), "tipo": t,
                "modelo": MODELOS.get(t) or r.get("desc") or t}

    def new_flight(self, h, rd, st):
        origin, origin_approx = None, False
        if st.get("aeropuerto") and st.get("ground_ts") and rd["ts"] - st["ground_ts"] < 48 * 3600:
            origin = st["aeropuerto"]
        elif rd["alt"] < 8000:
            ap, d = nearest_airport(rd["lat"], rd["lon"])
            if ap and _d(d) < 40:
                origin, origin_approx = ap, True
        climb_min = min(rd["alt"] / 2000.0, 25)
        dep = rd["ts"] - climb_min * 60
        if st.get("ground_ts") and st["ground_ts"] > dep:
            dep = st["ground_ts"]
        f = self.ident(h)
        f.update({
            "id": f"{h}-{int(dep)}", "callsign": rd["callsign"],
            "origen": origin, "origen_aprox": origin_approx,
            "destino": None, "destino_aprox": False,
            "salida": iso(dep), "salida_aprox": climb_min > 3,
            "llegada": None, "llegada_aprox": False,
            "estado": "en_vuelo", "alt_max": rd["alt"],
            "track": [[rd["lat"], rd["lon"], rd["alt"], rd["ts"]]],
        })
        return f

    @staticmethod
    def add_point(fl, rd):
        tr = fl["track"]
        if tr and rd["ts"] - tr[-1][3] < 40:
            return
        tr.append([rd["lat"], rd["lon"], rd["alt"], rd["ts"]])
        fl["alt_max"] = max(fl.get("alt_max", 0), rd["alt"])
        if rd["callsign"]:
            fl["callsign"] = rd["callsign"]

    def close(self, fl, dest, dest_approx, arr_ts, arr_approx, estado="aterrizado"):
        fl.update(destino=dest, destino_aprox=dest_approx, llegada=iso(arr_ts),
                  llegada_aprox=arr_approx, estado=estado)
        o = fl.get("origen")
        if o and dest:
            fl["distancia_km"] = round(haversine_km(o["lat"], o["lon"], dest["lat"], dest["lon"]))
        fl["duracion_min"] = max(1, round((arr_ts - parse_iso(fl["salida"])) / 60))
        track = fl.pop("track", [])
        self.tracks[fl["id"]] = thin(track, STORE_TRACK_POINTS)
        if fl["id"] in self.by_id:
            self.by_id[fl["id"]].update(fl)
        else:
            self.hist.append(fl)
            self.by_id[fl["id"]] = fl

    def reopen(self, fid):
        f = self.by_id.pop(fid, None)
        if not f:
            return None
        self.hist.remove(f)
        f["track"] = self.tracks.pop(fid, [])
        f.update(estado="en_vuelo", llegada=None, destino=None)
        f.pop("duracion_min", None)
        f.pop("distancia_km", None)
        return f

    def close_after_gap(self, st, cur, name):
        """Cierra el vuelo en curso tras un hueco en el que el avión aterrizó sin que lo viéramos."""
        lp = cur["track"][-1]
        if lp[2] < LOW_ALT_FT:
            ap, d = nearest_airport(lp[0], lp[1])
            arr = lp[3] + min(lp[2] / 1500.0, 15) * 60
            self.close(cur, ap, _d(d) > 25, arr, True)
            print(f"  {name}: hueco de datos; aterrizaje deducido en {(ap or {}).get('icao', '?')}")
        else:
            self.close(cur, None, True, lp[3], True, estado="sin_senal")
            print(f"  {name}: hueco de datos; vuelo cerrado sin destino")
        st.update(ultimo_cerrado=cur["id"], vuelo=None, fase="desconocida", aeropuerto=None)

    def split_gaps(self):
        """Corta los vuelos en curso que juntan varios vuelos por huecos de datos: lo anterior al
        último hueco imposible se cierra y el vuelo en curso empieza tras ese hueco."""
        for h, st in self.state["aviones"].items():
            cur = st.get("vuelo")
            tr = (cur or {}).get("track") or []
            cut = None
            for i in range(1, len(tr)):
                p = tr[i]
                if not same_flight(tr[i - 1], {"ts": p[3], "lat": p[0], "lon": p[1], "alt": p[2]}):
                    cut = i
            if cut is None:
                continue
            name = self.ident(h)["matricula"]
            old = dict(cur, track=tr[:cut])
            self.close_after_gap(st, old, name)
            p = tr[cut]
            new = self.new_flight(h, {"ts": p[3], "lat": p[0], "lon": p[1], "alt": p[2],
                                      "callsign": cur.get("callsign") or ""}, {})
            new.update(track=tr[cut:], alt_max=max(q[2] for q in tr[cut:]))
            st.update(vuelo=new, fase="aire", aeropuerto=None)
            print(f"  {name}: vuelo partido por un hueco de datos; el actual empieza {new['salida']}")

    def can_reopen(self, prev, rd):
        """Un vuelo cerrado «sin señal» se reabre solo si la nueva lectura puede ser su continuación."""
        if rd["ts"] - parse_iso(prev["llegada"]) > REOPEN_MAX_HOURS * 3600:
            return False
        tr = self.tracks.get(prev["id"]) or []
        return not tr or same_flight(tr[-1], rd)

    def fill_origin(self, h, cur, st, name):
        """Si no se vio el despegue, saca el origen del recorrido del día publicado por adsb.lol."""
        if cur.get("origen") or not cur.get("track"):
            return
        now = now_ts()
        first_ts = cur["track"][0][3]
        if now - first_ts > 20 * 3600 or now - st.get("traza_ts", 0) < TRACE_RETRY_MIN * 60:
            return
        st["traza_ts"] = now
        res = takeoff_from_trace(fetch_trace(h), first_ts)
        if not res:
            print(f"  {name}: origen aún sin encontrar en el recorrido")
            return
        ap, approx, dep = res
        cur.update(origen=ap, origen_aprox=approx)
        if first_ts - 20 * 3600 < dep <= first_ts + 120:
            cur.update(salida=iso(dep), salida_aprox=approx)
        print(f"  {name}: origen recuperado del recorrido: {ap['icao']}{' (aprox.)' if approx else ''}")

    def process(self, h, ac, answered, t):
        st = self.state["aviones"].setdefault(h, {"fase": "desconocida"})
        cur = st.get("vuelo")
        rd = reading_from(ac) if ac else None
        if rd and st.get("ultima") and rd["ts"] <= st["ultima"]["ts"]:
            rd = None
        name = self.ident(h)["matricula"]

        if rd:
            st["ultima"] = rd
            if rd["airborne"]:
                if cur is not None and cur.get("track") and not same_flight(cur["track"][-1], rd):
                    self.close_after_gap(st, cur, name)   # aterrizó y volvió a despegar sin verlo
                    cur = None
                if cur is None:
                    prev = self.by_id.get(st.get("ultimo_cerrado") or "")
                    if prev and prev["estado"] == "sin_senal" and self.can_reopen(prev, rd):
                        cur = self.reopen(prev["id"])
                        print(f"  {name}: recupera señal")
                    else:
                        cur = self.new_flight(h, rd, st)
                        print(f"  {name}: DESPEGUE desde {(cur['origen'] or {}).get('icao', '?')}")
                    st.update(vuelo=cur, fase="aire", aeropuerto=None)
                self.add_point(cur, rd)
                self.fill_origin(h, cur, st, name)
            else:
                ap, d = nearest_airport(rd["lat"], rd["lon"])
                if cur is not None:
                    lp = cur["track"][-1] if cur.get("track") else None
                    if lp and rd["ts"] - lp[3] > GAP_MIN * 60:
                        # hueco antes de verlo en tierra: no sabemos cuándo aterrizó
                        lap, ld = nearest_airport(lp[0], lp[1])
                        if lp[2] < LOW_ALT_FT and lap and ap and lap["icao"] != ap["icao"] and _d(ld) < 25:
                            # venía bajando hacia otro aeropuerto: ese fue el destino; lo de después no se vio
                            self.close_after_gap(st, cur, name)
                        else:
                            arr = est_arrival(lp, rd["lat"], rd["lon"], rd["ts"] - 60)
                            self.close(cur, ap, _d(d) > 15, arr, True)
                            st.update(ultimo_cerrado=cur["id"], vuelo=None)
                            print(f"  {name}: ATERRIZAJE (tras hueco, hora aprox.) en {(ap or {}).get('icao', '?')}")
                    else:
                        self.add_point(cur, rd)
                        self.close(cur, ap, _d(d) > 15, rd["ts"] - 60, False)
                        st["ultimo_cerrado"] = cur["id"]
                        st["vuelo"] = None
                        print(f"  {name}: ATERRIZAJE en {(ap or {}).get('icao', '?')}")
                else:
                    prev = self.by_id.get(st.get("ultimo_cerrado") or "")
                    if prev and prev["estado"] == "sin_senal" and ap and _d(d) < 15:
                        lt = parse_iso(prev["llegada"])
                        lp = (self.tracks.get(prev["id"]) or [None])[-1]
                        if rd["ts"] - lt < FILL_GAP_HOURS * 3600 and (not lp or lp[2] >= LOW_ALT_FT):
                            arr = est_arrival(lp, ap["lat"], ap["lon"], rd["ts"] - 60) if lp else lt
                            prev.update(destino=ap, destino_aprox=False, estado="aterrizado", llegada_aprox=True,
                                        llegada=iso(arr),
                                        duracion_min=max(1, round((arr - parse_iso(prev["salida"])) / 60)))
                            o = prev.get("origen")
                            if o:
                                prev["distancia_km"] = round(haversine_km(o["lat"], o["lon"], ap["lat"], ap["lon"]))
                st.update(fase="tierra", aeropuerto=ap if ap and _d(d) < 15 else None, ground_ts=rd["ts"])
        elif answered and cur is not None and cur["track"]:
            lp = cur["track"][-1]
            gap_min = (t - lp[3]) / 60
            if lp[2] < LOW_ALT_FT and gap_min > LANDED_LOST_MIN:
                ap, d = nearest_airport(lp[0], lp[1])
                arr = lp[3] + min(lp[2] / 1500.0, 15) * 60
                self.close(cur, ap, _d(d) > 25, arr, True)
                st.update(vuelo=None, ultimo_cerrado=cur["id"], fase="tierra",
                          aeropuerto=ap if ap and _d(d) < 25 else None, ground_ts=round(arr))
                print(f"  {name}: aterrizaje deducido en {(ap or {}).get('icao', '?')}")
            elif gap_min > LOST_CLOSE_HOURS * 60:
                self.close(cur, None, True, lp[3], True, estado="sin_senal")
                st.update(vuelo=None, ultimo_cerrado=cur["id"], fase="desconocida", aeropuerto=None)
                print(f"  {name}: sin señal, vuelo cerrado")

    def prune(self, days):
        cutoff = now_ts() - days * 86400
        # fuera también los «vuelos» imposibles (> 20 h) que dejaron los huecos de datos antiguos
        keep = [f for f in self.hist if parse_iso(f["salida"]) >= cutoff
                and (f.get("duracion_min") or 0) <= MAX_FLIGHT_MIN]
        gone = {f["id"] for f in self.hist} - {f["id"] for f in keep}
        for fid in gone:
            self.by_id.pop(fid, None)
            self.tracks.pop(fid, None)
        self.hist = keep


# ------------------------------------------------------------------ asignaciones

def make_matcher(assign):
    rules = []
    for a in assign.get("asignaciones", []):
        try:
            d0 = date.fromisoformat(a["desde"]) if a.get("desde") else date.min
            d1 = date.fromisoformat(a["hasta"]) if a.get("hasta") else date.max
        except ValueError:
            continue
        rules.append(((a.get("hex") or "").lower(), (a.get("matricula") or "").upper(), d0, d1))
    inc = set(assign.get("incluir_vuelos", []))
    exc = set(assign.get("excluir_vuelos", []))

    def match(f):
        if f["id"] in exc:
            return False
        if f["id"] in inc:
            return True
        d = local_date(parse_iso(f["salida"]))
        for h, m, d0, d1 in rules:
            if (h and f["hex"] == h) or (m and f.get("matricula", "").upper() == m):
                if d0 <= d <= d1:
                    return True
        return False
    return match


def update_her(store, assign):
    match = make_matcher(assign)
    path = os.path.join(DATA, "ella.json")
    her = {f["id"]: f for f in load(path, {}).get("vuelos", [])}
    window_start = min([parse_iso(f["salida"]) for f in store.hist] or [now_ts()])
    # quitar los que ya no cumplen (si cambió la asignación)
    for fid in list(her):
        if not match(her[fid]):
            del her[fid]
    # añadir/actualizar desde la flota (vuelos cerrados recientes y en curso)
    for f in store.hist:
        if match(f):
            g = dict(f)
            g["track"] = store.tracks.get(f["id"]) or her.get(f["id"], {}).get("track", [])
            her[f["id"]] = g
    for st in store.state["aviones"].values():
        cur = st.get("vuelo")
        if cur and match(cur):
            her[cur["id"]] = dict(cur)
    # un vuelo que figuraba "en vuelo" y ya no está ni en curso ni cerrado: lo dejamos tal cual
    vuelos = sorted(her.values(), key=lambda f: f["salida"])
    save(path, {"actualizado": iso(now_ts()), "etiqueta": assign.get("etiqueta", "Ella"),
                "ventana_flota_desde": iso(window_start), "vuelos": vuelos})
    return vuelos


# ------------------------------------------------------------------ salidas

def write_flota(cfg, store, t):
    out = []
    for h, r in store.state["registro"].items():
        st = store.state["aviones"].get(h, {})
        last = st.get("ultima")
        cur = st.get("vuelo")
        prev = store.by_id.get(st.get("ultimo_cerrado") or "")
        item = store.ident(h)
        item.update({
            "fase": st.get("fase", "desconocida"),
            "aeropuerto": st.get("aeropuerto"),
            "ultima": last,
            "callsign": r.get("callsign"),
        })
        if cur:
            v = {k: cur.get(k) for k in ("id", "origen", "origen_aprox", "salida", "salida_aprox", "callsign")}
            v["track"] = [p[:3] for p in thin(cur["track"], LIVE_TRACK_POINTS)]
            item["vuelo"] = v
        if prev:
            item["ultimo_vuelo"] = {k: prev.get(k) for k in ("id", "origen", "destino", "salida", "llegada", "estado")}
        out.append(item)
    out.sort(key=lambda a: a["matricula"])
    save(os.path.join(DATA, "flota.json"), {
        "actualizado": iso(t), "operador": cfg.get("operador", {}).get("nombre", ""), "aviones": out})


def ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    if len(line.encode("utf-8")) <= 74:
        return line
    parts, cur = [], b""
    for ch in line:
        e = ch.encode("utf-8")
        if len(cur) + len(e) > 73:
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += e
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def ap_label(ap):
    return (ap.get("iata") or ap.get("icao")) if ap else "?"


def ap_long(ap):
    if not ap:
        return "desconocido"
    city = (ap.get("ciudad") or "").split(",")[0]
    return f"{ap.get('nombre')} ({ap.get('icao')}){' · ' + city if city and city not in ap.get('nombre', '') else ''}"


def write_ics(cfg, assign, vuelos, t):
    def dt(s):
        return s.replace("-", "").replace(":", "")
    label = assign.get("etiqueta", "Ella")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//vuelos-tracker//ES", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape('✈️ Vuelos de ' + label)}",
             "X-WR-TIMEZONE:Europe/Madrid", "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    stamp = dt(iso(t))
    for f in vuelos:
        if parse_iso(f["salida"]) < t - 400 * 86400:
            continue
        if f.get("llegada"):
            end = f["llegada"]
        else:
            last = f["track"][-1][3] if f.get("track") else parse_iso(f["salida"])
            end = iso(max(last + 45 * 60, t + 30 * 60))
        o, d = f.get("origen"), f.get("destino")
        if f["estado"] == "en_vuelo":
            title = f"✈️ {ap_label(o)} → … (en vuelo)"
        elif f["estado"] == "sin_senal":
            title = f"✈️ {ap_label(o)} → ? (sin señal)"
        else:
            title = f"✈️ {ap_label(o)} → {ap_label(d)}"
        desc = [f"{f['matricula']} · {f.get('modelo', '')}{(' · ' + f['callsign']) if f.get('callsign') else ''}",
                f"Salida: {ap_long(o)}{' (hora aprox.)' if f.get('salida_aprox') else ''}",
                f"Llegada: {ap_long(d) if d else '—'}{' (aprox.)' if f.get('llegada_aprox') else ''}"]
        if f.get("duracion_min"):
            h, m = divmod(f["duracion_min"], 60)
            desc.append(f"Duración: {h} h {m:02d} min")
        if f.get("distancia_km"):
            desc.append(f"Distancia: {f['distancia_km']} km")
        desc.append(f"Flightradar24: https://www.flightradar24.com/data/aircraft/{f['matricula'].lower()}")
        desc.append(f"ADS-B Exchange: https://globe.adsbexchange.com/?icao={f['hex']}")
        lines += ["BEGIN:VEVENT", f"UID:{f['id']}@vuelos-tracker", f"DTSTAMP:{stamp}",
                  f"DTSTART:{dt(f['salida'])}", f"DTEND:{dt(end)}", f"SUMMARY:{ics_escape(title)}",
                  f"LOCATION:{ics_escape(ap_long(o) if o else '')}",
                  f"DESCRIPTION:{ics_escape(chr(10).join(desc))}",
                  f"URL:https://www.flightradar24.com/data/aircraft/{f['matricula'].lower()}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(os.path.join(DATA, "vuelos.ics"), "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(ics_fold(l) for l in lines) + "\r\n")


def write_all(cfg, store):
    t = now_ts()
    assign = load(ASSIGN, {"asignaciones": []})
    store.prune(cfg.get("dias_historial_flota", 30))
    store.hist.sort(key=lambda f: f["salida"])
    save(os.path.join(DATA, "historial_flota.json"), store.hist)
    save(os.path.join(DATA, "_tracks.json"), store.tracks)
    write_flota(cfg, store, t)
    vuelos = update_her(store, assign)
    write_ics(cfg, assign, vuelos, t)
    store.state["actualizado"] = iso(t)
    save(os.path.join(DATA, "state.json"), store.state)


# ------------------------------------------------------------------ main

def main():
    os.makedirs(DATA, exist_ok=True)
    cfg = load(CONFIG, None)
    if not cfg:
        sys.exit("falta config.json")
    store = Store()
    store.split_gaps()
    start = last_push = now_ts()
    n = 0
    while True:
        t0 = now_ts()
        readings, answered = poll(cfg, store.state)
        n += 1
        for h in set(store.state["registro"]) | set(readings):
            store.process(h, readings.get(h), h in answered or h in readings, now_ts())
        air = sum(1 for s in store.state["aviones"].values() if s.get("fase") == "aire")
        print(f"[{iso(t0)}] consulta {n}: {len(readings)} con señal, {air} en el aire, "
              f"{len(store.state['registro'])} en la flota conocida")
        elapsed = now_ts() - start
        if elapsed + POLL_EVERY > MAX_SECONDS:
            break
        if PUSH_CMD and now_ts() - last_push >= PUSH_EVERY:
            write_all(cfg, store)
            subprocess.run(PUSH_CMD, shell=True)
            last_push = now_ts()
        time.sleep(max(0, POLL_EVERY - (now_ts() - t0)))
    write_all(cfg, store)
    print("datos escritos")


if __name__ == "__main__":
    main()
