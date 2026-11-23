#!/usr/bin/env python3
"""Földgáz-adatgyűjtő a "Gáztározók" fülhöz (gaz.html). Kimenet: gas.json.

Két forrás, egymástól függetlenül (az egyik hibája nem viszi el a másikat):

1. GIE AGSI+ (https://agsi.gie.eu/api): a tárolók töltöttsége, a tárolt mennyiség (TWh), a munkagáz-kapacitás,
   a betáplálás/kivétel (GWh/nap) és ezek kapacitása. Magyarország, az EU összesen és a szomszédos/fontos országok.
   Ingyenes személyes kulcs kell hozzá (https://agsi.gie.eu regisztráció), környezeti változó: GIE_API_KEY,
   a kulcs a `x-key` fejlécben megy. Korlát: 60 hívás/perc, ezért a hívások között ~1,1 s szünet van,
   és ha már van gas.json, csak az utolsó napok töltődnek újra.
2. ENTSOG Transparency Platform (https://transparency.entsog.eu/api/v1): a magyar határpontok fizikai gázáramlása
   (indikátor: "Physical Flow", napi, kWh/nap -> GWh/nap). Kulcs nem kell. A határpontokat futáskor maga
   deríti fel az /interconnections végpontról (a magyar üzemeltető oldalán), nincs bedrótozott kulcslista.

Futtatás:
    export GIE_API_KEY="a-te-kulcsod"
    python gas.py --out gas.json

A kulcs csak a környezeti változóban él, a böngészőbe nem kerül.
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

AGSI_BASE = "https://agsi.gie.eu/api"
ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"

COUNTRY_NAMES = {
    "HU": "Magyarország", "AT": "Ausztria", "SK": "Szlovákia", "RO": "Románia", "HR": "Horvátország",
    "CZ": "Csehország", "DE": "Németország", "IT": "Olaszország", "PL": "Lengyelország", "BG": "Bulgária",
    "NL": "Hollandia", "FR": "Franciaország", "UA": "Ukrajna", "RS": "Szerbia", "SI": "Szlovénia",
    "EU": "EU összesen",
}
# sorrend = fontossági sorrend (a legfontosabbak először)
AGSI_COUNTRIES = ["HU", "AT", "SK", "RO", "HR", "UA", "DE", "IT", "CZ", "PL", "BG", "NL", "FR"]
FULL_FIELDS = ("full", "gis", "wgv", "inj", "wdr", "injCap", "wdrCap", "trend")   # HU és EU
LITE_FIELDS = ("full", "gis", "wgv")                                               # a többi ország
AGSI_KEYS = {"full": "full", "gis": "gasInStorage", "wgv": "workingGasVolume", "inj": "injection",
             "wdr": "withdrawal", "injCap": "injectionCapacity", "wdrCap": "withdrawalCapacity", "trend": "trend"}
DEC = {"full": 1, "gis": 3, "wgv": 3, "inj": 1, "wdr": 1, "injCap": 1, "wdrCap": 1, "trend": 3}
DEFAULT_FROM = "2021-10-01"      # a 2021/22-es gázév kezdete


# ------------------------------------------------------------------ HTTP
class Http:
    """Vékony réteg a requests fölött, hogy a tesztben kitalált klienssel lehessen helyettesíteni.
    get(url, params, headers) -> kiértékelt JSON; hibára kivételt dob (status_code attribútummal)."""

    def get(self, url, params=None, headers=None, timeout=60):
        import requests
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            err = RuntimeError(f"HTTP {r.status_code}")
            err.status_code = r.status_code
            raise err
        return r.json()


class Throttle:
    """Legalább `gap` másodperc két hívás indítása között (AGSI+: 60 hívás/perc)."""

    def __init__(self, gap=1.1):
        self.gap, self.last, self.lock = gap, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            d = self.last + self.gap - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.last = time.monotonic()


def num(v):
    """AGSI+: a számok idézőjeles szövegek, a hiányt "-" vagy null jelzi."""
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def status_of(exc):
    return getattr(exc, "status_code", None)


# ------------------------------------------------------------------ AGSI+
def agsi_fetch(http, key, throttle, params, deadline, errors, label):
    """Az összes oldal egy lekérdezéshez. Visszaadja a rekordok listáját (vagy üreset)."""
    rows, page = [], 1
    while True:
        if time.monotonic() > deadline:
            errors.append(f"AGSI {label}: időkeret lejárt")
            break
        p = dict(params, page=page, size=300)
        data = None
        for attempt in (1, 2):
            throttle.wait()
            try:
                data = http.get(AGSI_BASE, params=p, headers={"x-key": key})
                break
            except Exception as exc:  # noqa: BLE001
                st = status_of(exc)
                if st in (401, 403):
                    sys.exit(f"AGSI+ HTTP {st}: érvénytelen vagy hiányzó GIE_API_KEY.")
                if st == 429 and attempt == 1:
                    time.sleep(61)         # a limit túllépése 60 másodperces blokkot ad
                    continue
                if attempt == 1:
                    time.sleep(3)
                    continue
                errors.append(f"AGSI {label}: {exc.__class__.__name__} {exc}")
        if data is None:
            break
        rows += data.get("data") or []
        last = int(data.get("last_page") or 1)
        if page >= last:
            break
        page += 1
    return rows


def parse_rows(rows, fields):
    out = {}
    for r in rows:
        d = str(r.get("gasDayStart") or "")[:10]
        if len(d) != 10:
            continue
        out[d] = {f: num(r.get(AGSI_KEYS[f])) for f in fields}
        out[d]["status"] = r.get("status")
    return out


def flatten_children(rec, depth=0):
    """A legutóbbi országos rekord gyermekei (társaságok, esetleg telephelyek) laposan."""
    res = []
    for ch in rec.get("children") or []:
        res.append({
            "name": ch.get("name"), "code": ch.get("code"), "level": depth,
            "full": num(ch.get("full")), "gis": num(ch.get("gasInStorage")), "wgv": num(ch.get("workingGasVolume")),
            "inj": num(ch.get("injection")), "wdr": num(ch.get("withdrawal")),
            "injCap": num(ch.get("injectionCapacity")), "wdrCap": num(ch.get("withdrawalCapacity")),
            "status": ch.get("status"),
        })
        if depth < 1:
            res += flatten_children(ch, depth + 1)
    return res


def collect_agsi(http, key, existing=None, from_date=DEFAULT_FROM, today=None, budget_s=420, throttle=None):
    today = today or date.today()
    throttle = throttle or Throttle()
    deadline = time.monotonic() + budget_s
    errors = []
    old = (existing or {}).get("series") or {}
    old_dates = (existing or {}).get("dates") or []
    # inkrementális: ha a régi adat lefedi a kezdetet, csak az utolsó ~14 napot kérjük újra
    inc_from = None
    if old_dates and old_dates[0] <= from_date:
        inc_from = (datetime.strptime(old_dates[-1], "%Y-%m-%d").date() - timedelta(days=14)).isoformat()
    start = inc_from or from_date

    per = {}                     # kód -> {dátum: értékek}
    facilities, hu_latest, upd = [], None, None
    for code in ["EU"] + AGSI_COUNTRIES:
        fields = FULL_FIELDS if code in ("HU", "EU") else LITE_FIELDS
        params = {"type": "eu"} if code == "EU" else {"country": code}
        params.update({"from": start, "to": today.isoformat()})
        rows = agsi_fetch(http, key, throttle, params, deadline, errors, code)
        if not rows:
            continue
        per[code] = parse_rows(rows, fields)
        if code == "HU":
            newest = max(rows, key=lambda r: str(r.get("gasDayStart")))
            facilities = flatten_children(newest)
            hu_latest = str(newest.get("gasDayStart"))[:10]
            upd = newest.get("updatedAt")

    if not per.get("HU"):
        return None, errors
    # régi + új összefésülése (az új felülír)
    merged = {}
    for code, fields in [(c, FULL_FIELDS if c in ("HU", "EU") else LITE_FIELDS) for c in ["EU"] + AGSI_COUNTRIES]:
        m = {}
        if code in old and old_dates:
            for i, d in enumerate(old_dates):
                if d >= from_date:
                    m[d] = {f: (old[code].get(f) or [None] * len(old_dates))[i] for f in fields}
        if code in per:
            m.update(per[code])
        if m:
            merged[code] = m
    dates = sorted({d for m in merged.values() for d in m if d >= from_date})
    series = {}
    for code, m in merged.items():
        fields = FULL_FIELDS if code in ("HU", "EU") else LITE_FIELDS
        series[code] = {f: [None if m.get(d, {}).get(f) is None else round(m[d][f], DEC[f]) for d in dates] for f in fields}
    return {
        "dates": dates, "series": series,
        "names": {c: COUNTRY_NAMES.get(c, c) for c in series},
        "hu_last_day": hu_latest, "hu_updated": upd,
        "facilities": facilities,
    }, errors


# ------------------------------------------------------------------ ENTSOG
def entsog_list(js):
    """Az ENTSOG-válasz rekordlistája; a kulcs neve (operationaldatas / operationalData ...) kisbetűsítve keresendő."""
    if isinstance(js, list):
        return js
    for k, v in js.items():
        if isinstance(v, list) and k.lower() in ("operationaldatas", "operationaldata", "interconnections", "data"):
            return v
    for v in js.values():
        if isinstance(v, list):
            return v
    return []


def discover_points(http, errors):
    """A magyar határpontok és irányaik a /interconnections végpontról.
    Visszatér: [{"id","label","country","side":"entry|exit","op","point"}] ; side a magyar üzemeltető oldala."""
    try:
        js = http.get(f"{ENTSOG_BASE}/interconnections", params={"limit": -1}, timeout=90)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ENTSOG pontfelderítés: {exc.__class__.__name__} {exc}")
        return []
    pts, seen = [], set()
    for r in entsog_list(js):
        fc, tc = str(r.get("fromCountryKey") or ""), str(r.get("toCountryKey") or "")
        pk = r.get("pointKey")
        if not pk or "HU" not in (fc, tc) or fc == tc:
            continue
        if fc == "HU":
            side, op, nb = "exit", r.get("fromOperatorKey"), tc
        else:
            side, op, nb = "entry", r.get("toOperatorKey"), fc
        if not op or (op, pk, side) in seen:
            continue
        seen.add((op, pk, side))
        pts.append({"id": f"{pk}|{side}", "label": r.get("pointLabel") or pk, "country": nb, "side": side,
                    "op": op, "point": pk})
    if not pts:
        errors.append("ENTSOG pontfelderítés: nem találtam magyar határpontot a válaszban")
    return pts


def collect_flows(http, days=400, today=None, budget_s=300, workers=4):
    today = today or date.today()
    errors, notes = [], []
    pts = discover_points(http, errors)
    if not pts:
        return None, errors, notes
    deadline = time.monotonic() + budget_s
    start = today - timedelta(days=days)
    windows, a = [], start
    while a <= today:
        b = min(a + timedelta(days=119), today)
        windows.append((a, b))
        a = b + timedelta(days=1)
    chunks = [pts[i:i + 8] for i in range(0, len(pts), 8)]

    def job(win, chunk):
        if time.monotonic() > deadline:
            return "skip", []
        pd_ = ",".join(f"{p['op']}{p['point']}{p['side']}" for p in chunk)
        params = {"indicator": "Physical Flow", "periodType": "day", "pointDirection": pd_,
                  "from": win[0].isoformat(), "to": win[1].isoformat(), "limit": -1, "timezone": "CET"}
        last = None
        for attempt in (1, 2):
            try:
                return "ok", entsog_list(http.get(f"{ENTSOG_BASE}/operationaldatas", params=params, timeout=90))
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(3 * attempt)
        return f"err:{last.__class__.__name__} {last}", []

    futs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for w in windows:
            for c in chunks:
                futs.append(ex.submit(job, w, c))
    vals = {}                                    # id -> {nap: GWh/nap}
    skipped = 0
    for f in futs:
        st, recs = f.result()
        if st == "skip":
            skipped += 1
        elif st.startswith("err"):
            errors.append(f"ENTSOG áramlás: {st[4:]}")
        for r in recs:
            if str(r.get("indicator", "Physical Flow")) != "Physical Flow":
                continue
            v = num(r.get("value"))
            day = str(r.get("periodFrom") or "")[:10]
            pid = f"{r.get('pointKey')}|{r.get('directionKey')}"
            if v is None or len(day) != 10:
                continue
            unit = str(r.get("unit") or "kWh/d")
            k = 1e-6 if unit.lower().startswith("kwh") else 1e-3 if unit.lower().startswith("mwh") else 1.0
            vals.setdefault(pid, {})[day] = vals.setdefault(pid, {}).get(day, 0.0) + v * k
    if skipped:
        notes.append(f"ENTSOG: az időkeret lejárt, kihagyott kérések: {skipped}")
    pts = [p for p in pts if p["id"] in vals]
    if not pts:
        errors.append("ENTSOG: a felderített pontokra nem érkezett fizikai áramlási adat")
        return None, errors, notes
    alld = sorted({d for p in pts for d in vals[p["id"]]})
    return {
        "days": alld,
        "points": [{"id": p["id"], "label": p["label"], "country": p["country"], "side": p["side"],
                    "gwh": [None if vals[p["id"]].get(d) is None else round(vals[p["id"]][d], 2) for d in alld]}
                   for p in sorted(pts, key=lambda q: (q["country"], q["label"], q["side"]))],
        "unit": "GWh/nap",
    }, errors, notes


# ------------------------------------------------------------------ összerakás
def collect(http, key, existing=None, today=None, budget_s=420, from_date=DEFAULT_FROM, throttle=None):
    today = today or date.today()
    errors, notes = [], []
    out = {
        "schema": 1, "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "GIE AGSI+; ENTSOG Transparency Platform", "demo": False,
        "agsi": None, "flows": None, "errors": errors, "notes": notes,
    }
    if key:
        try:
            agsi, e = collect_agsi(http, key, (existing or {}).get("agsi"), from_date, today, budget_s, throttle)
            errors += e
            out["agsi"] = agsi
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AGSI+: {exc.__class__.__name__} {exc}")
    else:
        errors.append("AGSI+: nincs GIE_API_KEY, a tározói adatok kimaradnak")
    try:
        flows, e, n = collect_flows(http, today=today)
        errors += e
        notes += n
        out["flows"] = flows
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ENTSOG: {exc.__class__.__name__} {exc}")
    # ha egy rész most nem sikerült, a korábbi jó adat megmarad (jelezve)
    for part in ("agsi", "flows"):
        if out[part] is None and (existing or {}).get(part):
            out[part] = existing[part]
            notes.append(f"{part}: most nem sikerült frissíteni, az előző futás adata maradt")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="gas.json")
    ap.add_argument("--from-date", default=DEFAULT_FROM)
    ap.add_argument("--budget", type=int, default=420, help="az AGSI+ rész időkerete, másodpercben")
    args = ap.parse_args()
    key = os.environ.get("GIE_API_KEY", "").strip()
    existing = None
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            existing = None
    out = collect(Http(), key, existing, budget_s=args.budget, from_date=args.from_date)
    if not out["agsi"] and not out["flows"]:
        for e in out["errors"]:
            print("  -", e)
        sys.exit("Sem tározói, sem áramlási adat nem érkezett; a gas.json nem íródik felül.")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    a = out["agsi"]
    print(f"Kész: {args.out} | tározó: {'igen, ' + str(len(a['dates'])) + ' nap, ' + str(len(a['series'])) + ' sorozat' if a else 'nincs'}"
          f" | határpont-áramlás: {'igen, ' + str(len(out['flows']['points'])) + ' pont' if out['flows'] else 'nincs'}"
          f" | hibák: {len(out['errors'])}")
    for e in out["errors"] + out["notes"]:
        print("  -", e)


if __name__ == "__main__":
    main()
