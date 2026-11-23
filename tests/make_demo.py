"""Szintetikus DEMO adat az oldal előnézetéhez. NEM valós mérés.

A collector.py saját collect() függvényén át készül egy kitalált ENTSO-E klienssel,
ezért a séma pontosan megegyezik az éles data.json-éval. A kimenetben "demo": true.
"""
import importlib.util, pathlib
import json
import sys

import numpy as np
import pandas as pd
from entsoe.exceptions import NoMatchingDataError

spec = importlib.util.spec_from_file_location("collector", str(pathlib.Path(__file__).resolve().parent.parent / "collector.py"))
col = importlib.util.module_from_spec(spec)
spec.loader.exec_module(col)

OUT = sys.argv[1]
NOW = pd.Timestamp("2026-11-23 13:20", tz="Europe/Budapest").tz_convert("UTC")
rng = np.random.default_rng(7)


def local_hour(idx):
    return idx.tz_convert("Europe/Budapest").hour + idx.tz_convert("Europe/Budapest").minute / 60


# nettó irány (pozitív = import HU felé), MW
def net_pattern(nb, idx):
    h = local_hour(idx).to_numpy()
    solar = np.exp(-((h - 13) ** 2) / 12)          # déli naperőművi csúcs
    evening = np.exp(-((h - 19) ** 2) / 6)         # esti csúcsigény
    n = {
        "AT": 650 - 350 * solar + 300 * evening,
        "SK": 300 - 450 * solar + 150 * evening,
        "RO": -150 - 250 * solar + 350 * evening,
        "HR": -250 - 120 * solar + 100 * evening,
        "SI": 80 + 60 * evening - 40 * solar,
        "RS": 110 - 150 * solar + 120 * evening,
    }[nb]
    # determinisztikus zaj az időbélyegből: az import és az export hívás ugyanazt a nettó értéket látja
    t = idx.asi8 // 10**9
    return n + 22 * np.sin(t / 1700.0) + 12 * np.sin(t / 530.0)


class Demo:
    def query_crossborder_flows(self, a, b, start, end, **kw):
        nb = b if a == "HU" else a
        if nb == "UA":
            raise NoMatchingDataError
        lag = {"AT": 0, "SK": 1}.get(nb, 0)
        last = NOW.floor("h") - pd.Timedelta(hours=lag)
        freq = "15min" if nb == "AT" else "h"
        stop = last + (pd.Timedelta(minutes=45) if freq == "15min" else pd.Timedelta(0))
        idx = pd.date_range(start, stop, freq=freq)
        n = net_pattern(nb, idx)
        v = np.maximum(n, 0) if b == "HU" else np.maximum(-n, 0)   # import: nb->HU, export: HU->nb
        return pd.Series(v, index=idx.tz_convert("Europe/Brussels"))

    def query_day_ahead_prices(self, zone, start, end, **kw):
        if zone in ("UA", "RS"):
            raise NoMatchingDataError
        idx = pd.date_range(start, end, freq="15min", inclusive="left")
        h = local_hour(idx).to_numpy()
        day = (idx.tz_convert("Europe/Budapest").normalize() - start.normalize()).days.to_numpy()
        base = 95 + 12 * np.sin(day * 1.7)
        shape = 70 * np.exp(-((h - 19) ** 2) / 5) + 35 * np.exp(-((h - 8) ** 2) / 4) - 115 * np.exp(-((h - 13) ** 2) / 7)
        off = {"HU": 0, "AT": -7, "SK": 4, "RO": 14, "HR": 3, "SI": -2}[zone]
        return pd.Series(base + shape + off, index=idx.tz_convert("Europe/Brussels")).round(2)


d = col.collect(Demo(), now=NOW)
d["demo"] = True
d["source"] = "DEMO – szintetikus adat, nem valós mérés"
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
print("ok", OUT, "| utolsó teljes óra:", d["last_complete_ts"], "| hibák:", d["errors"])
