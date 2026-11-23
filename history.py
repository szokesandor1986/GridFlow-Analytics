#!/usr/bin/env python3
"""Több éves, havi összesítésű adatsorok az "Európa és trendek" oldalhoz (history.json).

Ritkán fut (hetente, külön workflow), mert évekre visszamenőleg tölt le, de az eredménynek csak a havi
összesítését őrzi meg (kb. 100-200 KB). Inkrementális: a már lezárt éveket nem tölti le újra.

Havi összesítések (helyi, budapesti idő szerinti hónapok):
  - a magyar termelés típusonként (GWh), a nap-részarány, a nap csúcsteljesítménye,
  - fogyasztás: energia, csúcs, minimum; éves terhelési tartamgörbe,
  - maradék terhelés (fogyasztás - nap - szél): déli minimum, esti rámpa (99. percentilis, MW/h),
  - day-ahead ár: átlag, szórás, negatív órák, déli és esti átlagár,
  - nettó import határonként és összesen (GWh), az import a fogyasztás %-ában,
  - szomszédos és fő európai zónák: átlagár, negatív órák, korreláció a magyar árral, azonos árú órák aránya,
  - éves beépített teljesítmény típusonként.

Használat:
    export ENTSOE_TOKEN=...
    python history.py --out history.json [--from-year 2019] [--workers 8] [--budget 1500]
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

import collector as col
from collector import HOME, NEIGHBOURS, Batch, flatten_types, hourly_mean

BUD = "Europe/Budapest"
# A magyar ár összehasonlítása: a collector.EUROPE_ZONES zónái (ha egy zónára nincs adat, kimarad).
ZONES = list(col.EUROPE_ZONES)
LDC_Q = list(range(100, -1, -5))        # terhelési tartamgörbe: 100%, 95% ... 0% kvantilis (csúcstól a minimumig)
SCHEMA = 1


def local_hours_in_month(period):
    s = period.start_time.tz_localize(BUD)
    e = (period + 1).start_time.tz_localize(BUD)
    return (e - s) / pd.Timedelta(hours=1)


def to_local(s):
    s = s.copy()
    s.index = s.index.tz_convert(BUD)
    return s


def group_series(by_type):
    """{ENTSO-E típus: órás Series} -> {csoport: Series}. Ismeretlen típus az "egyéb"-be kerül."""
    res = {}
    for t, s in by_type.items():
        g = col.TYPE_TO_GROUP.get(t, "other")
        res[g] = res[g].add(s, fill_value=0) if g in res else s
    return res


def f1(x, nd=1):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def monthly_mean_energy_gwh(s, period):
    """A hónap energiája GWh-ban: az elérhető órák átlaga szorozva a hónap óraszámával (hiányzó órák nem nullák).
    A lefedettség 50% alatt nem számolunk."""
    n = local_hours_in_month(period)
    if len(s) < 0.5 * n:
        return None
    return float(s.mean()) * n / 1000.0


def process_year(year, raw, now):
    """Egy év nyers (órás) adatából havi rekordok. raw: load, gen(by_type), price{zone}, imp{code}, exp{code}."""
    months = {}
    load = raw.get("load")
    gen = raw.get("gen") or {}
    groups = group_series(gen) if gen else {}
    solar = groups.get("solar")
    wind = groups.get("wind")
    prices = raw.get("price") or {}
    hu_p = prices.get(HOME)

    resid = None
    if load is not None and solar is not None:
        resid = load.reindex(load.index) - solar.reindex(load.index)
        if wind is not None:
            resid = resid - wind.reindex(load.index).fillna(0)
        resid = resid.dropna()

    # a helyi hónapok listája, amelyekre van bármilyen adat
    srcs = [x for x in (load, hu_p, *groups.values()) if x is not None and len(x)]
    if not srcs:
        return months
    splits = {}
    keys = sorted({k for s in srcs for k in to_local(s).index.strftime("%Y-%m").unique()})
    cur = pd.Period(now.tz_convert(BUD).strftime("%Y-%m"), "M")
    for key in keys:
        per = pd.Period(key, "M")
        if per.year != year:
            continue
        rec = {"partial": bool(per >= cur)}

        def cut(s):
            if s is None or len(s) == 0:
                return None
            sp = splits.get(id(s))
            if sp is None:                      # egy sorozatot egyszer bontunk hónapokra
                ls = to_local(s)
                sp = {k: g for k, g in ls.groupby(ls.index.strftime("%Y-%m"))}
                splits[id(s)] = sp
            r = sp.get(key)
            return r if r is not None and len(r) else None

        # --- termelés
        gen_rec = {}
        for g, s in groups.items():
            r = cut(s)
            if r is not None:
                v = monthly_mean_energy_gwh(r, per)
                if v is not None:
                    gen_rec[g] = f1(v, 1)
        rec["gen"] = gen_rec
        rs = cut(solar)
        rec["solar_peak"] = f1(rs.max(), 0) if rs is not None else None

        # --- fogyasztás
        rl = cut(load)
        if rl is not None:
            rec["load_gwh"] = f1(monthly_mean_energy_gwh(rl, per), 1)
            rec["load_peak"], rec["load_min"], rec["load_avg"] = f1(rl.max(), 0), f1(rl.min(), 0), f1(rl.mean(), 0)
        else:
            rec["load_gwh"] = rec["load_peak"] = rec["load_min"] = rec["load_avg"] = None

        # --- maradék terhelés
        rr = cut(resid)
        if rr is not None and len(rr) > 48:
            h = rr.index.hour
            mid = rr[(h >= 9) & (h <= 16)]
            rec["res_min_mid"] = f1(mid.min(), 0) if len(mid) else None
            d = rr.diff()
            ok = d.index.to_series().diff() == pd.Timedelta(hours=1)           # csak a szomszédos órák különbsége
            eve = d[ok & (d.index.hour >= 15) & (d.index.hour <= 21) & (d > 0)]
            rec["ramp_p99"] = f1(np.percentile(eve, 99), 0) if len(eve) >= 20 else None
        else:
            rec["res_min_mid"] = rec["ramp_p99"] = None

        # --- ár
        rp = cut(hu_p)
        if rp is not None and len(rp) >= 24:
            h = rp.index.hour
            mid = rp[(h >= 10) & (h <= 15)]
            eve = rp[(h >= 18) & (h <= 21)]
            rec["price"] = {
                "avg": f1(rp.mean(), 1), "min": f1(rp.min(), 1), "max": f1(rp.max(), 1), "std": f1(rp.std(), 1),
                "neg_h": int((rp < 0).sum()),
                "mid": f1(mid.mean(), 1) if len(mid) else None, "eve": f1(eve.mean(), 1) if len(eve) else None,
                "n": int(len(rp)),
            }
        else:
            rec["price"] = None

        # --- import
        border, total, have = {}, 0.0, False
        for code in NEIGHBOURS:
            i_, e_ = cut(raw.get("imp", {}).get(code)), cut(raw.get("exp", {}).get(code))
            if i_ is None and e_ is None:
                continue
            idx = (i_.index if i_ is not None else e_.index).union(e_.index if e_ is not None else i_.index)
            net = (i_.reindex(idx).fillna(0) if i_ is not None else 0) - (e_.reindex(idx).fillna(0) if e_ is not None else 0)
            n = local_hours_in_month(per)
            if len(idx) < 0.5 * n:
                continue
            v = float(net.mean()) * n / 1000.0
            border[code] = f1(v, 1)
            total += v
            have = True
        rec["border"] = border
        rec["imp_gwh"] = f1(total, 1) if have else None

        # --- zónák
        zrec = {}
        hp = cut(hu_p)
        for zone, zs in prices.items():
            rz = cut(zs)
            if rz is None or len(rz) < 24:
                continue
            z = {"avg": f1(rz.mean(), 1), "neg": int((rz < 0).sum())}
            if zone != HOME and hp is not None:
                j = pd.concat([rz, hp], axis=1, join="inner").dropna()
                if len(j) >= 200 and j.iloc[:, 0].std() > 0 and j.iloc[:, 1].std() > 0:
                    z["corr"] = f1(j.iloc[:, 0].corr(j.iloc[:, 1]), 3)
                    z["same"] = f1(100.0 * ((j.iloc[:, 0] - j.iloc[:, 1]).abs() < 0.01).mean(), 1)
            zrec[zone] = z
        rec["z"] = zrec
        months[key] = rec
    return months


def ldc_of(load):
    """Éves terhelési tartamgörbe: 21 érték, a csúcstól a minimumig (MW)."""
    if load is None or len(load) < 24 * 200:
        return None
    return [f1(np.percentile(load, q), 0) for q in LDC_Q]


def installed_by_year(df):
    out = {}
    if df is None:
        return out
    for ts, row in df.dropna(how="all").iterrows():
        tmp = {}
        for c, v in row.items():
            name = c[0] if isinstance(c, tuple) else c
            if pd.notna(v):
                g = col.TYPE_TO_GROUP.get(name, "other")
                tmp[g] = tmp.get(g, 0.0) + float(v)
        out[str(ts.year)] = {k: round(v, 0) for k, v in tmp.items()}
    return out


def collect_history(client, now=None, from_year=2019, existing=None, budget_s=1500, workers=8):
    now = now or pd.Timestamp.now(tz="UTC")
    existing = existing or {}
    hist = {
        "schema": SCHEMA, "months": dict(existing.get("months", {})), "annual": dict(existing.get("annual", {})),
        "years_done": list(existing.get("years_done", [])),
    }
    this_year = now.tz_convert(BUD).year
    # A lezárt évet február 1-ig még újra letöltjük (utólagos adatjavítások); a folyó évet mindig.
    todo = [y for y in range(from_year, this_year + 1) if y not in hist["years_done"] or y == this_year]
    todo = sorted(set(todo), reverse=True)           # a legfrissebb év az első: ha az időkeret kifogy, az régebbi marad ki

    B = Batch(budget_s, workers=workers)
    rng = {}
    for y in todo:
        s = pd.Timestamp(f"{y}-01-01", tz=BUD)
        e = min(pd.Timestamp(f"{y + 1}-01-01", tz=BUD), now.ceil("h"))
        rng[y] = (s, e)
        B.add(f"fogyasztás {y}", lambda s=s, e=e: client.query_load(HOME, start=s, end=e))
        B.add(f"termelés {y}", lambda s=s, e=e: client.query_generation(HOME, start=s, end=e, nett=True))
        for z in ZONES:
            B.add(f"ár {z} {y}", lambda z=z, s=s, e=e: client.query_day_ahead_prices(z, start=s, end=e))
        for code in NEIGHBOURS:
            B.add(f"import {code} {y}", lambda code=code, s=s, e=e: client.query_crossborder_flows(code, HOME, start=s, end=e))
            B.add(f"export {code} {y}", lambda code=code, s=s, e=e: client.query_crossborder_flows(HOME, code, start=s, end=e))
    first = pd.Timestamp(f"{min(todo)}-01-01", tz=BUD) if todo else None
    if todo:
        B.add("beépített teljesítmény", lambda: client.query_installed_generation_capacity(HOME, start=first, end=now))

    problems = []
    for y in todo:
        L = B.get(f"fogyasztás {y}")
        G = B.get(f"termelés {y}")
        raw = {
            "load": hourly_mean(L.iloc[:, 0] if isinstance(L, pd.DataFrame) else L) if L is not None else None,
            "gen": flatten_types(G) if G is not None else None,
            "price": {}, "imp": {}, "exp": {},
        }
        for z in ZONES:
            p = B.get(f"ár {z} {y}")
            if p is not None:
                raw["price"][z] = hourly_mean(p)
        for code in NEIGHBOURS:
            for tag, store in (("import", "imp"), ("export", "exp")):
                f = B.get(f"{tag} {code} {y}")
                if f is not None:
                    raw[store][code] = hourly_mean(f)
        if raw["load"] is None and not raw["gen"]:
            problems.append(f"{y}: sem a fogyasztás, sem a termelés nem érkezett meg; az év kimarad")
            continue
        recs = process_year(y, raw, now)
        for k, r in recs.items():
            hist["months"][k] = r
        ldc = ldc_of(raw["load"])
        a = hist["annual"].setdefault(str(y), {})
        if ldc:
            a["ldc"] = ldc
            a["ldc_partial"] = y >= this_year
        # lezárt: ha az év véget ért és már február van, nem töltjük le újra
        full = now.tz_convert(BUD) >= pd.Timestamp(f"{y + 1}-02-01", tz=BUD)
        if full and y not in hist["years_done"]:
            hist["years_done"].append(y)
    inst = installed_by_year(B.get("beépített teljesítmény")) if todo else {}
    for y, v in inst.items():
        hist["annual"].setdefault(y, {})["inst"] = v
    B.close()

    hist["months"] = dict(sorted(hist["months"].items()))
    hist["annual"] = dict(sorted(hist["annual"].items()))
    hist["years_done"] = sorted(set(hist["years_done"]))
    hist["generated_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hist["source"] = "ENTSO-E Transparency Platform"
    hist["demo"] = False
    hist["zones"] = ZONES
    hist["names"] = col.EUROPE_ZONES
    hist["errors"] = problems + B.errors + B.notes()
    hist["missing"] = B.missing
    hist["co2"] = {"factors": col.CO2_FACTORS, "source": col.CO2_SOURCE}
    return hist


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="history.json")
    p.add_argument("--from-year", type=int, default=2019)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--budget", type=int, default=1500, help="időkeret másodpercben")
    args = p.parse_args()

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        sys.exit("Hiányzik az ENTSOE_TOKEN környezeti változó.")
    from entsoe import EntsoePandasClient

    client = EntsoePandasClient(api_key=token, retry_count=2, retry_delay=5, timeout=90)
    existing = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            existing = {}
    t0 = time.monotonic()
    hist = collect_history(client, from_year=args.from_year, existing=existing, budget_s=args.budget, workers=args.workers)
    if not hist["months"]:
        sys.exit("Nem készült egyetlen havi rekord sem; a history.json nem íródik felül.")
    col.write_json(args.out, hist)
    print(f"Kész: {args.out} | hónapok: {len(hist['months'])} | lezárt évek: {hist['years_done']} | "
          f"hibák: {len(hist['errors'])} | {time.monotonic() - t0:.0f} s")
    for e in hist["errors"]:
        print("  -", e)


if __name__ == "__main__":
    main()
