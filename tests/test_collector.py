"""A collector.py tesztje kitalált klienssel, hálózat nélkül.  Futtatás:  python -I tests/test_collector.py"""
import importlib.util
import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd
from entsoe.exceptions import NoMatchingDataError

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

spec = importlib.util.spec_from_file_location("collector", ROOT / "collector.py")
col = importlib.util.module_from_spec(spec)
spec.loader.exec_module(col)
from fake_client import FakeClient  # noqa: E402

NOW = pd.Timestamp("2026-11-23 14:20", tz="UTC")


# ---------------------------------------------------------------- 1. fő adatok, kézzel ellenőrizhető értékekkel
class Stub:
    def query_crossborder_flows(self, a, b, start, end, **kw):
        nb = b if a == "HU" else a
        if nb == "UA":
            raise NoMatchingDataError
        if nb == "RS" and a == "HU":
            raise NoMatchingDataError                      # csak az egyik irány hiányzik
        lag = {"AT": 0, "SK": 1, "RO": 0, "HR": 0, "SI": 0, "RS": 0}[nb]
        last = NOW.floor("h") - pd.Timedelta(hours=lag)
        freq = "15min" if nb == "AT" else "h"
        idx = pd.date_range(start, last + (pd.Timedelta(minutes=45) if freq == "15min" else pd.Timedelta(0)), freq=freq, tz="UTC")
        base = {"AT": 700, "SK": 300, "RO": 200, "HR": 150, "SI": 100, "RS": 120}[nb]
        val = base if b == "HU" else base / 4
        return pd.Series(val, index=idx.tz_convert("Europe/Brussels"))

    def query_day_ahead_prices(self, zone, start, end, **kw):
        if zone in ("UA", "RS"):
            raise NoMatchingDataError
        idx = pd.date_range(start, end, freq="15min", inclusive="left")
        return pd.Series(100.0 + len(zone), index=idx.tz_convert("Europe/Brussels"))


def test_main_numbers():
    d = col.collect(Stub(), now=NOW)
    json.dumps(d)
    H = dict(zip(d["hourly_ts"], zip(d["net_import_mw"], d["complete"])))
    assert abs(H["2026-11-23T13:00:00Z"][0] - 1207.5) < 1e-6 and H["2026-11-23T13:00:00Z"][1] is True
    assert d["last_complete_ts"] == "2026-11-23T13:00:00Z"
    assert H["2026-11-23T14:00:00Z"][1] is False
    assert "UA" not in d["prices"]["zones"] and "HU" in d["prices"]["zones"]
    assert d["prices"]["zones"]["HU"][0] == 102.0 and d["prices"]["zones"]["AT"][0] == 102.0
    print("1. fő adatok rendben")


# ---------------------------------------------------------------- 2. mérnöki adatok
def test_engineer():
    client = FakeClient(NOW)
    main = col.collect(client, now=NOW)
    e = col.collect_engineer(client, now=NOW, main=main)
    txt = json.dumps(e)
    assert "NaN" not in txt and "Infinity" not in txt, "a JSON-ban nem lehet NaN"
    n = len(e["hourly_ts"])
    assert e["hourly_ts"][0] == (NOW - pd.Timedelta(hours=48)).floor("h").strftime("%Y-%m-%dT%H:%M:%SZ")
    # holnap 24:00 helyi idő = 2026-11-24 23:00Z, az idősor ennél egy órával korábban ér véget
    assert e["hourly_ts"][-1] == "2026-11-24T22:00:00Z", e["hourly_ts"][-1]
    assert e["errors"] == [], e["errors"]
    for k, v in e["modules"].items():
        assert v, f"modul adat nélkül: {k}"

    # minden idősor ugyanolyan hosszú, mint az idő-tengely
    def lens(o, path=""):
        if isinstance(o, list) and o and not isinstance(o[0], dict):
            assert len(o) == n, (path, len(o), n)
        elif isinstance(o, dict):
            for k, v in o.items():
                lens(v, path + "/" + k)
    for k in ("flows", "load", "generation", "solar_wind_forecast", "imbalance", "balancing"):
        lens(e[k], k)
    for u in e["units"]:
        assert len(u["mw"]) == n

    # fizikai - menetrendezett = a kitalált nem menetrendezett áramlás (AT)
    T = pd.DatetimeIndex(pd.to_datetime(e["hourly_ts"], utc=True))
    from fake_client import unscheduled
    f = e["flows"]["AT"]
    ok = [i for i in range(n) if f["phys_net"][i] is not None and f["sched_net"][i] is not None]
    assert len(ok) > 40
    diff = np.array([f["phys_net"][i] - f["sched_net"][i] for i in ok])
    want = np.array([unscheduled("AT", T[[i]])[0] for i in ok])
    assert np.abs(diff - want).max() < 25, np.abs(diff - want).max()       # az órás átlag miatt kis eltérés lehet
    assert "UA" not in e["flows"]
    assert e["flows"]["RS"]["ntc_in"] is not None and e["flows"]["AT"]["ntc_in"] is None

    # a csoportosított atomtermelés = a blokkok összege
    nuc_units = [u for u in e["units"] if u["group"] == "nuclear"]
    assert len(nuc_units) == 4
    for i in ok[-5:]:
        s = sum(u["mw"][i] for u in nuc_units)
        assert abs(s - e["generation"]["nuclear"][i]) < 1e-3 * max(1, s) + 1, (s, e["generation"]["nuclear"][i])
    # a gáz-blokkok összege = gáz csoport
    gas_units = [u for u in e["units"] if u["group"] == "gas"]
    i = ok[-1]
    assert abs(sum(u["mw"][i] for u in gas_units) - e["generation"]["gas"][i]) < 2

    # a jövőben (holnap) csak előrejelzés van, tény nincs
    assert e["load"]["actual"][-1] is None and e["load"]["forecast"][-1] is not None
    assert e["solar_wind_forecast"]["solar"][-1] is not None and e["generation"]["solar"][-1] is None

    # kiesések: a törölt és az elavult változat nem szerepel; a nem tervezett igen
    names = [(o["name"], o["planned"], o["end"]) for o in e["outages_gen"]]
    assert not any(o["name"] == "Lignit blokk" for o in e["outages_gen"]), names
    b = [o for o in e["outages_gen"] if o["name"] == "Gázblokk B"]
    assert len(b) == 1 and b[0]["planned"] is False and b[0]["end"] == (NOW + pd.Timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert any(o["border"] == "SK" for o in e["outages_tx"]) and any(o["border"] == "RS" and not o["planned"] for o in e["outages_tx"])
    assert e["installed_mw"]["solar"] == 8500 and e["installed_mw"]["nuclear"] == 2000
    assert set(e["co2"]["factors"]) == set(col.GROUPS)
    print("2. mérnöki adatok rendben | méret:", len(txt), "bájt | hiányzó lekérdezések:", len(e["missing"]))


def test_survives_broken_client():
    """Ha a kliens minden mérnöki lekérdezésre hibázik, a collect_engineer nem dől el, hanem hibát jegyez."""
    class Broken(FakeClient):
        def query_load(self, *a, **k):
            raise RuntimeError("szimulált hiba")
        query_generation = query_load
    c = Broken(NOW)
    e = col.collect_engineer(c, now=NOW, main=None)
    assert e["load"]["actual"] is None and e["load"]["forecast"] is not None and not e["modules"]["generation"]
    assert any("RuntimeError" in x for x in e["errors"])
    assert e["modules"]["units"]
    print("3. hibatűrés rendben")


def test_budget():
    """Lejárt időkeretnél a mérnöki lekérdezések kimaradnak, a hívás nem dől el, és a hiba látszik."""
    class Counting(FakeClient):
        calls = 0
        def query_load(self, *a, **k):
            Counting.calls += 1
            return super().query_load(*a, **k)
    c = Counting(NOW)
    e = col.collect_engineer(c, now=NOW, main=None, budget_s=-1)
    assert Counting.calls == 0 and not any(e["modules"].values())
    assert any("időkeret" in x for x in e["errors"]), e["errors"]
    json.dumps(e)
    print("4. időkeret rendben")


def test_parallel():
    """Lassú ENTSO-E mellett a párhuzamos lekérdezés a sorosnál sokkal gyorsabb, és az eredmény ugyanaz."""
    import time as _t

    class Slow(FakeClient):
        pass
    for name in [n for n in dir(FakeClient) if n.startswith("query_")]:
        def make(n):
            def f(self, *a, **k):
                _t.sleep(0.05)
                return getattr(FakeClient, n)(self, *a, **k)
            return f
        setattr(Slow, name, make(name))
    t0 = _t.monotonic()
    e = col.collect_engineer(Slow(NOW), now=NOW, main=None, budget_s=60, workers=8)
    dt = _t.monotonic() - t0
    ref = col.collect_engineer(FakeClient(NOW), now=NOW, main=None, budget_s=60, workers=1)
    assert json.dumps(e["load"]) == json.dumps(ref["load"]) and e["modules"] == ref["modules"]
    # sorosan ~70 kérés x 0,05 s = 3,5 s lenne
    assert dt < 2.0, dt
    print(f"5. párhuzamosság rendben ({dt:.2f} s)")


def test_europe():
    """Európai ár-pillanatkép (europe.json)."""
    eu = col.collect_europe(FakeClient(NOW), now=NOW, budget_s=60)
    assert "HU" in eu["zones"] and "DE_LU" in eu["zones"] and "RS" not in eu["zones"], sorted(eu["zones"])
    assert len(eu["ts"]) >= 24 * 3 and all(len(v) == len(eu["ts"]) for v in eu["zones"].values())
    json.dumps(eu, allow_nan=False)
    print(f"6. európai ár-pillanatkép rendben ({len(eu['zones'])} zóna)")


if __name__ == "__main__":
    # a with_retry várakozását kikapcsoljuk, hogy a hibatesztek ne lassúak legyenek
    col.time.sleep = lambda s: None
    test_main_numbers()
    test_engineer()
    test_survives_broken_client()
    test_budget()
    test_parallel()
    test_europe()
    print("MINDEN TESZT ÁTMENT")
