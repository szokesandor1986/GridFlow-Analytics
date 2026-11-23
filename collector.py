#!/usr/bin/env python3
"""ENTSO-E adatgyűjtő a magyar határkeresztező villamosenergia-áramlás oldalhoz.

Lekéri (ENTSO-E Transparency Platform, RESTful API):
  - a HU és a szomszédos zónák közötti fizikai áramlást (MW), mindkét irányban,
  - a HU és a szomszédos zónák day-ahead árát (EUR/MWh),
és egy data.json fájlba írja, amit az index.html olvas.

A "mérnöki" adatkörök (menetrend, kapacitás, fogyasztás, termelés típusonként és blokkonként,
nap/szél előrejelzés, kiesések, rendszeregyensúly) egy külön engineer.json fájlba kerülnek,
amit a muszaki.html olvas. Ennek hibája nem akadályozza a data.json frissítését.

Futtatás:
    pip install "entsoe-py>=1.0.0" pandas
    export ENTSOE_TOKEN="a-te-tokened"
    python collector.py --out data.json

A token csak ebben a szkriptben él, a böngészőbe soha nem kerül.
Egy-egy határ vagy zóna hibája nem állítja le a futást: a hiba bekerül a data.json "errors" listájába.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd

HOME = "HU"
# kód -> magyar név, a határ fizikai áramlásához
NEIGHBOURS = {
    "AT": "Ausztria",
    "SK": "Szlovákia",
    "UA": "Ukrajna",
    "RO": "Románia",
    "RS": "Szerbia",
    "HR": "Horvátország",
    "SI": "Szlovénia",
}
PRICE_ZONES = [HOME] + list(NEIGHBOURS)
# Határonkénti nap előtti NTC csak a nem-Core határokon van (a Core-határokon flow-based kapacitás-kiosztás van,
# ott nincs NTC, a lekérdezés felesleges és lassú). A magyar Core-határok: AT, SK, RO, HR, SI.
NTC_BORDERS = ("RS", "UA")


def with_retry(fn, tries=3, wait=4):
    """Átmeneti hálózati hibánál újrapróbál; 401/403-nál azonnal leáll értelmes üzenettel."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                sys.exit(f"HTTP {status}: érvénytelen token vagy nincs RESTful API jogosultság.")
            if exc.__class__.__name__ in NO_DATA:
                raise
            last = exc
            if attempt < tries:
                time.sleep(wait * attempt)
    raise last


NO_DATA = ("NoMatchingDataError", "InvalidBusinessParameterError", "InvalidPSRTypeError")


class Batch:
    """Lekérdezések párhuzamosan. A futásidőt az ENTSO-E válaszideje adja, nem a számolás,
    ezért több kérés fut egyszerre (a limit 400 kérés/perc, mi összesen is csak ~100 kérést küldünk).
    A kérések beadási sorrendje a fontossági sorrend; a határidő után a még nem indult kérések kimaradnak."""

    def __init__(self, budget_s, workers=8, tries=2, wait=2):
        self.deadline = time.monotonic() + budget_s
        self.budget_s = budget_s
        self.ex = ThreadPoolExecutor(max_workers=workers)
        self.futs = {}
        self.tries, self.wait = tries, wait
        self.errors, self.missing, self.skipped = [], [], 0

    def _run(self, fn):
        if time.monotonic() > self.deadline:
            return "skip", None
        try:
            v = with_retry(fn, tries=self.tries, wait=self.wait)
        except Exception as exc:  # noqa: BLE001
            name = exc.__class__.__name__
            return ("nodata" if name in NO_DATA else "err:" + name), None
        if v is None or (hasattr(v, "__len__") and len(v) == 0):
            return "nodata", None
        return "ok", v

    def add(self, key, fn):
        self.futs[key] = self.ex.submit(self._run, fn)

    def get(self, key):
        """Az eredmény (vagy None). A hiányt és a hibát feljegyzi. 401/403 esetén a SystemExit továbbmegy."""
        st, v = self.futs[key].result()
        if st == "ok":
            return v
        if st == "skip":
            self.skipped += 1
        elif st == "nodata":
            self.missing.append(key)
        else:
            self.errors.append(f"{key}: {st[4:]}")
        return None

    def close(self):
        self.ex.shutdown(wait=True)

    def notes(self):
        return [f"időkeret ({self.budget_s} s) lejárt, kihagyott lekérdezések: {self.skipped}"] if self.skipped else []


def to_list(series):
    """Pandas sorozat -> JSON-barát lista (NaN -> None), 2 tizedesre kerekítve."""
    return [None if pd.isna(v) else round(float(v), 2) for v in series]


def iso(index):
    return [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in index]


def hourly_mean(s):
    return s.tz_convert("UTC").resample("h").mean()


def collect(client, now=None, hours=48, budget_s=240, workers=8):
    now = now or pd.Timestamp.now(tz="UTC")
    start = (now - pd.Timedelta(hours=hours)).floor("h")
    end = now.ceil("h") + pd.Timedelta(hours=1)
    B = Batch(budget_s, workers=workers)

    # ---- kérések beadása (fontossági sorrendben), majd az eredmények összegyűjtése
    local_midnight = now.tz_convert("Europe/Budapest").normalize()
    p_start = local_midnight - pd.Timedelta(days=1)
    p_end = local_midnight + pd.Timedelta(days=2)
    for code in NEIGHBOURS:
        for direction, a, b in (("import", code, HOME), ("export", HOME, code)):
            B.add(f"áramlás {code} {direction}", lambda a=a, b=b: client.query_crossborder_flows(a, b, start=start, end=end))
    for zone in PRICE_ZONES:
        B.add(f"ár {zone}", lambda zone=zone: client.query_day_ahead_prices(zone, start=p_start, end=p_end))

    # ---- fizikai áramlás
    imp, exp = {}, {}
    for code, name in NEIGHBOURS.items():
        for direction, store in (("import", imp), ("export", exp)):
            s_ = B.get(f"áramlás {code} {direction}")
            if s_ is not None:
                store[code] = hourly_mean(s_)

    idx = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
    imp_df = pd.DataFrame(imp).reindex(idx)
    exp_df = pd.DataFrame(exp).reindex(idx)
    # Egy határ egy órában "jelen van", ha legalább az egyik irányra van érték
    # (a fizikai áramlást irányonként teszik közzé; a másik irány tipikusan 0 vagy nincs publikálva).
    cols = list(NEIGHBOURS)
    have_any = (imp_df.reindex(columns=cols).notna() | exp_df.reindex(columns=cols).notna())
    active = [c for c in cols if have_any[c].any()]

    # "teljes óra": minden aktív határnak van adata -> ezt használja a fő szám
    complete = have_any[active].all(axis=1) if active else pd.Series(False, index=idx)
    net = imp_df.sum(axis=1, skipna=True) - exp_df.sum(axis=1, skipna=True)
    net[~have_any.any(axis=1)] = float("nan")

    borders = []
    for code, name in NEIGHBOURS.items():
        i = imp_df[code] if code in imp_df.columns else pd.Series(float("nan"), index=idx)
        e = exp_df[code] if code in exp_df.columns else pd.Series(float("nan"), index=idx)
        have = have_any[code]
        last_ts = have[have].index.max() if have.any() else None
        borders.append(
            {
                "code": code,
                "name": name,
                "import_mw": to_list(i),
                "export_mw": to_list(e),
                "last_ts": None if last_ts is None else last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    last_complete = complete[complete].index.max() if complete.any() else None

    # ---- day-ahead árak
    prices = {}
    for zone in PRICE_ZONES:
        s_ = B.get(f"ár {zone}")
        if s_ is not None:
            prices[zone] = s_.tz_convert("UTC")
    B.close()
    errors = B.errors + B.notes()

    price_block = {"ts": [], "zones": {}}
    if prices:
        pidx = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for s in prices.values()])))
        price_block["ts"] = iso(pidx)
        for zone, s in prices.items():
            price_block["zones"][zone] = to_list(s.reindex(pidx))

    return {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ENTSO-E Transparency Platform",
        "demo": False,
        "home": HOME,
        "hourly_ts": iso(idx),
        "net_import_mw": to_list(net),
        "complete": [bool(x) for x in complete],
        "last_complete_ts": None if last_complete is None else last_complete.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "borders": borders,
        "prices": price_block,
        "errors": errors,
    }


# ======================================================================================
#  MÉRNÖKI ADATKÖRÖK  (engineer.json)
# ======================================================================================

# ENTSO-E termelési típus -> csoport
GROUPS = {
    "nuclear": ["Nuclear"],
    "gas": ["Fossil Gas", "Fossil Coal-derived gas"],
    "coal": ["Fossil Brown coal/Lignite", "Fossil Hard coal", "Fossil Peat", "Fossil Oil shale"],
    "solar": ["Solar"],
    "wind": ["Wind Onshore", "Wind Offshore"],
    "bio": ["Biomass", "Waste"],
    "hydro": ["Hydro Run-of-river and poundage", "Hydro Water Reservoir", "Hydro Pumped Storage"],
    "other": ["Fossil Oil", "Geothermal", "Other renewable", "Other", "Marine"],
}
TYPE_TO_GROUP = {t: g for g, ts in GROUPS.items() for t in ts}

# Életciklus-alapú kibocsátási tényezők, gCO2eq/kWh (IPCC AR5 WG3, Annex III, mediánok).
# Az "egyéb" csoportnak nincs egységes IPCC-értéke: 700 g/kWh az óvatos (magas) közelítés,
# a hulladék a biomassza értékét kapja. Mindkettő becslés, az oldalon is így szerepel.
CO2_FACTORS = {"nuclear": 12, "gas": 490, "coal": 820, "solar": 48, "wind": 11, "bio": 230, "hydro": 24, "other": 700}
CO2_SOURCE = "IPCC AR5 WG3 Annex III, életciklus-mediánok (gCO2eq/kWh); egyéb: 700, becslés"


def hourly_list(obj, idx):
    """Series (vagy 1 oszlopos DataFrame) -> órás átlag az idx-re igazítva, JSON-lista."""
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        obj = obj.iloc[:, 0]
    return to_list(hourly_mean(obj).reindex(idx))


def net_of(imp, exp, idx):
    """Két irány (import, export) órás sorozatából nettó: import - export (hiányzó irány = 0, ha a másik van)."""
    a = hourly_mean(imp).reindex(idx) if imp is not None else pd.Series(float("nan"), index=idx)
    b = hourly_mean(exp).reindex(idx) if exp is not None else pd.Series(float("nan"), index=idx)
    have = a.notna() | b.notna()
    n = a.fillna(0) - b.fillna(0)
    n[~have] = float("nan")
    return n


def physical_net_from_main(main, code, idx):
    """A data.json-ban már letöltött fizikai áramlásból nettó (import - export), az idx-re igazítva."""
    if not main:
        return None
    b = next((x for x in main["borders"] if x["code"] == code), None)
    if not b or not b.get("last_ts"):
        return None
    ts = pd.DatetimeIndex(pd.to_datetime(main["hourly_ts"], utc=True))
    i = pd.Series(b["import_mw"], index=ts, dtype="float64")
    e = pd.Series(b["export_mw"], index=ts, dtype="float64")
    have = i.notna() | e.notna()
    n = i.fillna(0) - e.fillna(0)
    n[~have] = float("nan")
    return n.reindex(idx)


def flatten_types(df):
    """Termelési DataFrame -> {típusnév: órás Series}; a 'fogyasztás' (szivattyúzás) oszlopokat kihagyja."""
    out = {}
    for c in df.columns:
        name, kind = (c[0], c[-1]) if isinstance(c, tuple) else (c, "Actual Aggregated")
        if "consumption" in str(kind).lower():
            continue
        s = hourly_mean(df[c])
        out[name] = out[name].add(s, fill_value=0) if name in out else s
    return out


def group_sum(by_type, idx):
    """{ENTSO-E típus: Series} -> {csoport: lista}; csak azok a csoportok, ahol van adat."""
    res = {}
    for g, types in GROUPS.items():
        parts = [by_type[t].reindex(idx) for t in types if t in by_type]
        if not parts:
            continue
        df = pd.concat(parts, axis=1)
        s = df.sum(axis=1, min_count=1)
        res[g] = to_list(s)
    # ismeretlen típus nem tűnhet el: az "egyéb"-hez adjuk
    known = set(TYPE_TO_GROUP)
    extra = [by_type[t].reindex(idx) for t in by_type if t not in known]
    if extra:
        s = pd.concat(extra, axis=1).sum(axis=1, min_count=1)
        if "other" in res:
            cur = pd.Series(res["other"], index=idx, dtype="float64")
            s = pd.concat([cur, s], axis=1).sum(axis=1, min_count=1)
        res["other"] = to_list(s)
    return res


def outage_rows(df, now, kind):
    """ENTSO-E kiesési dokumentumok -> tömör lista (legutóbbi változat, nem törölt, az ablakba eső)."""
    if df is None or not len(df):
        return []
    d = df.reset_index()
    if "mrid" in d and "revision" in d:
        d = d[d["revision"] == d.groupby("mrid")["revision"].transform("max")]
    if "docstatus" in d:
        d = d[~d["docstatus"].astype(str).str.lower().isin(["cancelled", "withdrawn"])]
    lo, hi = now - pd.Timedelta(days=7), now + pd.Timedelta(days=45)
    rows, seen = [], set()
    for _, r in d.iterrows():
        try:
            st, en = pd.to_datetime(r["start"], utc=True), pd.to_datetime(r["end"], utc=True)
        except Exception:  # noqa: BLE001
            continue
        if pd.isna(st) or pd.isna(en) or en < lo or st > hi:
            continue
        key = (str(r.get("mrid")), st, en)
        if key in seen:
            continue
        seen.add(key)
        bt = str(r.get("businesstype", ""))
        row = {
            "start": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": en.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "avail_mw": None if pd.isna(r.get("avail_qty")) else round(float(r["avail_qty"]), 1),
            "planned": "unplanned" not in bt.lower() and "forced" not in bt.lower(),
        }
        if kind == "gen":
            row["name"] = str(r.get("production_resource_name") or r.get("production_resource_psr_name") or "ismeretlen")
            row["type"] = str(r.get("plant_type") or "")
            row["group"] = TYPE_TO_GROUP.get(row["type"], "other")
            nom = r.get("nominal_power")
            row["nominal_mw"] = None if pd.isna(nom) else round(float(nom), 1)
        rows.append(row)
    rows.sort(key=lambda x: x["start"])
    return rows[:200]


def updown(df):
    """Szabályozási energia DataFrame -> (fel, le) Series; az oszlopnevekből ismeri fel az irányt."""
    up = dn = None
    for c in df.columns:
        n = " ".join(map(str, c)) if isinstance(c, tuple) else str(c)
        n = n.lower()
        if "up" in n and up is None:
            up = df[c]
        elif "down" in n and dn is None:
            dn = df[c]
    return up, dn


def collect_engineer(client, now=None, hours=48, main=None, budget_s=240, workers=8):
    now = now or pd.Timestamp.now(tz="UTC")
    start = (now - pd.Timedelta(hours=hours)).floor("h")
    local = now.tz_convert("Europe/Budapest").normalize()
    end = (local + pd.DateOffset(days=2)).tz_convert("UTC")      # holnap 24:00 (helyi idő): az előrejelzések miatt
    idx = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
    B = Batch(budget_s, workers=workers)
    ow_start, ow_end = now.floor("D") - pd.Timedelta(days=7), now.floor("D") + pd.Timedelta(days=45)
    cap_start = pd.Timestamp(year=now.year - 1, month=1, day=1, tz="UTC")

    # ---- kérések beadása, FONTOSSÁGI SORRENDBEN (ha az időkeret kifogy, a végén lévők maradnak ki):
    # 1. fogyasztás, termelés, előrejelzés, rendszeregyensúly  2. határok menetrendje, kapacitása
    # 3. blokkszintű termelés  4. kiesések (a leglassabbak)
    B.add("fogyasztás tény", lambda: client.query_load(HOME, start=start, end=end))
    B.add("fogyasztás előrejelzés", lambda: client.query_load_forecast(HOME, start=start, end=end))
    B.add("termelés típusonként", lambda: client.query_generation(HOME, start=start, end=end, nett=True))
    B.add("nap/szél előrejelzés", lambda: client.query_wind_and_solar_forecast(HOME, start=start, end=end))
    B.add("eltérési ár", lambda: client.query_imbalance_prices(HOME, start=start, end=end))
    B.add("eltérési mennyiség", lambda: client.query_imbalance_volumes(HOME, start=start, end=end))
    for bt, tag in (("A96", "afrr"), ("A97", "mfrr")):
        B.add(f"szabályozási energia {tag}", lambda bt=bt: client.query_activated_balancing_energy(HOME, start=start, end=end, business_type=bt))
    B.add("beépített teljesítmény", lambda: client.query_installed_generation_capacity(HOME, start=cap_start, end=now))
    for code in NEIGHBOURS:
        for da in (False, True):
            tag = " DA" if da else ""
            B.add(f"menetrend {code}→HU{tag}", lambda code=code, da=da: client.query_scheduled_exchanges(code, HOME, start=start, end=end, dayahead=da))
            B.add(f"menetrend HU→{code}{tag}", lambda code=code, da=da: client.query_scheduled_exchanges(HOME, code, start=start, end=end, dayahead=da))
    for code in NTC_BORDERS:
        B.add(f"NTC {code}→HU", lambda code=code: client.query_net_transfer_capacity_dayahead(code, HOME, start=start, end=end))
        B.add(f"NTC HU→{code}", lambda code=code: client.query_net_transfer_capacity_dayahead(HOME, code, start=start, end=end))
    B.add("blokkszintű termelés", lambda: client.query_generation_per_plant(HOME, start=start, end=end))
    B.add("termelő-kiesések", lambda: client.query_unavailability_of_generation_units(HOME, start=ow_start, end=ow_end))
    for code in NEIGHBOURS:
        for a_, b_ in ((code, HOME), (HOME, code)):
            B.add(f"hálózati kiesés {a_}→{b_}", lambda a_=a_, b_=b_: client.query_unavailability_transmission(a_, b_, start=ow_start, end=ow_end))
    A = lambda key, fn=None: B.get(key)  # noqa: E731  (az eredmények kiolvasása; a lekérdezés már fut)

    # ---- határonként: fizikai vs menetrendezett, kapacitás
    flows = {}
    for code in NEIGHBOURS:
        f = {"phys_net": None, "sched_net": None, "sched_da_net": None, "ntc_in": None, "ntc_out": None}
        phys = physical_net_from_main(main, code, idx)
        f["phys_net"] = None if phys is None else to_list(phys)
        for field, da in (("sched_net", False), ("sched_da_net", True)):
            i_ = A(f"menetrend {code}→HU{' DA' if da else ''}")
            e_ = A(f"menetrend HU→{code}{' DA' if da else ''}")
            if i_ is not None or e_ is not None:
                f[field] = to_list(net_of(i_, e_, idx))
        ci = A(f"NTC {code}→HU") if code in NTC_BORDERS else None
        co = A(f"NTC HU→{code}") if code in NTC_BORDERS else None
        f["ntc_in"] = hourly_list(ci, idx)
        f["ntc_out"] = hourly_list(co, idx)
        if any(v is not None for v in f.values()):
            flows[code] = f

    # ---- fogyasztás (tény + day-ahead előrejelzés)
    load = {"actual": hourly_list(A("fogyasztás tény", lambda: client.query_load(HOME, start=start, end=end)), idx),
            "forecast": hourly_list(A("fogyasztás előrejelzés", lambda: client.query_load_forecast(HOME, start=start, end=end)), idx)}

    # ---- termelés típusonként
    generation, by_type = {}, {}
    gdf = A("termelés típusonként", lambda: client.query_generation(HOME, start=start, end=end, nett=True))
    if gdf is not None:
        by_type = flatten_types(gdf)
        generation = group_sum(by_type, idx)

    # ---- nap / szél day-ahead előrejelzés
    sw = {"solar": None, "wind": None}
    fdf = A("nap/szél előrejelzés", lambda: client.query_wind_and_solar_forecast(HOME, start=start, end=end))
    if fdf is not None:
        ft = flatten_types(fdf)
        parts = {"solar": ["Solar"], "wind": ["Wind Onshore", "Wind Offshore"]}
        for k, names in parts.items():
            got = [ft[n].reindex(idx) for n in names if n in ft]
            if got:
                sw[k] = to_list(pd.concat(got, axis=1).sum(axis=1, min_count=1))

    # ---- beépített teljesítmény (a legutóbbi közölt év)
    installed = {}
    idf = A("beépített teljesítmény", lambda: client.query_installed_generation_capacity(HOME, start=pd.Timestamp(year=now.year - 1, month=1, day=1, tz="UTC"), end=now))
    if idf is not None:
        last = idf.dropna(how="all").iloc[-1]
        tmp = {}
        for c, v in last.items():
            name = c[0] if isinstance(c, tuple) else c
            if pd.notna(v):
                g = TYPE_TO_GROUP.get(name, "other")
                tmp[g] = tmp.get(g, 0.0) + float(v)
        installed = {k: round(v, 1) for k, v in tmp.items()}

    # ---- blokkszintű termelés
    units = []
    pdf = A("blokkszintű termelés", lambda: client.query_generation_per_plant(HOME, start=start, end=end))
    if pdf is not None:
        for c in pdf.columns:
            name = c[0] if isinstance(c, tuple) else str(c)
            typ = next((x for x in c[1:] if x in TYPE_TO_GROUP), "") if isinstance(c, tuple) else ""
            kind = c[-1] if isinstance(c, tuple) else ""
            if "consumption" in str(kind).lower():
                continue
            lst = hourly_list(pdf[c], idx)
            if lst and any(v is not None for v in lst):
                units.append({"name": str(name), "type": str(typ), "group": TYPE_TO_GROUP.get(str(typ), "other"), "mw": lst})
        units.sort(key=lambda u: -max(v for v in u["mw"] if v is not None))
        units = units[:40]

    # ---- kiesések
    ow_start, ow_end = now.floor("D") - pd.Timedelta(days=7), now.floor("D") + pd.Timedelta(days=45)
    outages_gen = outage_rows(A("termelő-kiesések", lambda: client.query_unavailability_of_generation_units(HOME, start=ow_start, end=ow_end)), now, "gen")
    outages_tx = []
    for code in NEIGHBOURS:
        for a, b in ((code, HOME), (HOME, code)):
            rows = outage_rows(A(f"hálózati kiesés {a}→{b}", lambda a=a, b=b: client.query_unavailability_transmission(a, b, start=ow_start, end=ow_end)), now, "tx")
            for r in rows:
                r["border"], r["from"], r["to"] = code, a, b
            outages_tx += rows
    outages_tx.sort(key=lambda x: x["start"])

    # ---- rendszeregyensúly
    imbalance = {"long": None, "short": None, "volume": None}
    ip = A("eltérési ár", lambda: client.query_imbalance_prices(HOME, start=start, end=end))
    if ip is not None:
        cols = {str(c).lower(): c for c in ip.columns}
        lc = next((v for k, v in cols.items() if "long" in k), None)
        sc = next((v for k, v in cols.items() if "short" in k), None)
        imbalance["long"] = hourly_list(ip[lc], idx) if lc is not None else None
        imbalance["short"] = hourly_list(ip[sc], idx) if sc is not None else None
        if lc is None and sc is None:
            imbalance["long"] = hourly_list(ip.iloc[:, 0], idx)
    iv = A("eltérési mennyiség", lambda: client.query_imbalance_volumes(HOME, start=start, end=end))
    if iv is not None:
        imbalance["volume"] = hourly_list(iv.iloc[:, 0] if isinstance(iv, pd.DataFrame) else iv, idx)
    balancing = {}
    for bt, tag in (("A96", "afrr"), ("A97", "mfrr")):
        bdf = A(f"szabályozási energia {tag}", lambda bt=bt: client.query_activated_balancing_energy(HOME, start=start, end=end, business_type=bt))
        if bdf is not None:
            up, dn = updown(bdf)
            balancing[f"{tag}_up"] = None if up is None else hourly_list(up, idx)
            balancing[f"{tag}_down"] = None if dn is None else hourly_list(dn, idx)

    B.close()
    errors = B.errors + B.notes()
    have = lambda v: bool(v) and (not isinstance(v, dict) or any(x is not None for x in v.values()))  # noqa: E731
    modules = {
        "flows": bool(flows),
        "sched": any(f.get("sched_net") is not None for f in flows.values()),
        "ntc": any(f.get("ntc_in") is not None or f.get("ntc_out") is not None for f in flows.values()),
        "load": have(load),
        "generation": bool(generation),
        "solar_wind_forecast": have(sw),
        "units": bool(units),
        "outages_gen": bool(outages_gen),
        "outages_tx": bool(outages_tx),
        "imbalance": have(imbalance),
        "balancing": have(balancing),
    }
    return {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ENTSO-E Transparency Platform",
        "demo": False,
        "hourly_ts": iso(idx),
        "flows": flows,
        "load": load,
        "generation": generation,
        "solar_wind_forecast": sw,
        "installed_mw": installed,
        "co2": {"factors": CO2_FACTORS, "source": CO2_SOURCE},
        "units": units,
        "outages_gen": outages_gen,
        "outages_tx": outages_tx,
        "imbalance": imbalance,
        "balancing": balancing,
        "modules": modules,
        "errors": errors,
        "missing": B.missing,
    }


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data.json")
    p.add_argument("--engineer-out", default=None, help="alapértelmezés: engineer.json a --out mellett")
    p.add_argument("--skip-engineer", action="store_true", help="a mérnöki adatkörök kihagyása")
    p.add_argument("--hours", type=int, default=48, help="ennyi órára visszamenőleg kéri az adatokat")
    p.add_argument("--workers", type=int, default=8, help="egyszerre ennyi ENTSO-E kérés fut")
    p.add_argument("--budget", type=int, default=240, help="a fő és a mérnöki rész időkerete külön-külön, másodpercben")
    args = p.parse_args()

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        sys.exit("Hiányzik az ENTSOE_TOKEN környezeti változó.")

    from entsoe import EntsoePandasClient

    # retry_delay: az alapértelmezett 60 s várakozás egy hibás kérés után percekre megállítaná a futást;
    # timeout: egy elakadt kérés ne tartson fel mindent.
    client = EntsoePandasClient(api_key=token, retry_count=2, retry_delay=5, timeout=45)
    data = collect(client, hours=args.hours, budget_s=args.budget, workers=args.workers)
    if not any(b["last_ts"] for b in data["borders"]):
        sys.exit("Egyik határra sem érkezett áramlási adat; a data.json nem íródik felül.")

    write_json(args.out, data)
    ok = [b["code"] for b in data["borders"] if b["last_ts"]]
    print(f"Kész: {args.out} | határok adattal: {ok} | hibák: {len(data['errors'])}")
    for e in data["errors"]:
        print("  -", e)

    if args.skip_engineer:
        return
    eng_out = args.engineer_out or os.path.join(os.path.dirname(os.path.abspath(args.out)), "engineer.json")
    try:
        eng = collect_engineer(client, hours=args.hours, main=data, budget_s=args.budget, workers=args.workers)
    except (Exception, SystemExit) as exc:  # a mérnöki rész hibája nem borítja a fő oldalt
        print(f"FIGYELEM: a mérnöki adatgyűjtés meghiúsult ({exc.__class__.__name__}: {exc}); az engineer.json nem íródik felül.")
        return
    if not any(eng["modules"].values()):
        print("FIGYELEM: a mérnöki adatkörökre semmi sem érkezett; az engineer.json nem íródik felül.")
        return
    write_json(eng_out, eng)
    got = [k for k, v in eng["modules"].items() if v]
    lack = [k for k, v in eng["modules"].items() if not v]
    print(f"Kész: {eng_out} | van adat: {got} | nincs: {lack} | hibák: {len(eng['errors'])}")
    for e in eng["errors"]:
        print("  -", e)


if __name__ == "__main__":
    main()
