import importlib.util, pathlib, json, numpy as np, pandas as pd
from entsoe.exceptions import NoMatchingDataError
spec = importlib.util.spec_from_file_location("collector", str(pathlib.Path(__file__).resolve().parent.parent / "collector.py"))
col = importlib.util.module_from_spec(spec); spec.loader.exec_module(col)

NOW = pd.Timestamp("2026-11-23 14:20", tz="UTC")

class Stub:
    def query_crossborder_flows(self, a, b, start, end, **kw):
        nb = b if a == "HU" else a
        if nb == "UA": raise NoMatchingDataError
        if nb == "RS" and a == "HU": raise NoMatchingDataError     # csak az egyik irány hiányzik
        lag = {"AT": 0, "SK": 1, "RO": 0, "HR": 0, "SI": 0, "RS": 0}[nb]
        last = NOW.floor("h") - pd.Timedelta(hours=lag)
        freq = "15min" if nb == "AT" else "h"
        idx = pd.date_range(start, last + (pd.Timedelta(minutes=45) if freq == "15min" else pd.Timedelta(0)), freq=freq, tz="UTC")
        base = {"AT": 700, "SK": 300, "RO": 200, "HR": 150, "SI": 100, "RS": 120}[nb]
        val = base if b == "HU" else base / 4          # import nagyobb, mint az export (konstans)
        return pd.Series(val, index=idx.tz_convert("Europe/Brussels"))
    def query_day_ahead_prices(self, zone, start, end, **kw):
        if zone in ("UA", "RS"): raise NoMatchingDataError
        idx = pd.date_range(start, end, freq="15min", inclusive="left")
        return pd.Series(100.0 + len(zone), index=idx.tz_convert("Europe/Brussels"))

d = col.collect(Stub(), now=NOW)
js = json.dumps(d)                                  # JSON-szerializálható-e
print("hibák:", d["errors"])
print("last_complete_ts:", d["last_complete_ts"], "(várt: SK 1 órás késése miatt 13:00Z)")
H = dict(zip(d["hourly_ts"], zip(d["net_import_mw"], d["complete"])))
for t in ("2026-11-23T12:00:00Z", "2026-11-23T13:00:00Z", "2026-11-23T14:00:00Z"):
    print(t, H[t])
# kézi ellenőrzés 13:00Z-ra: aktív határok AT,SK,RO,HR,SI,RS(import csak) -> import 700+300+200+150+100+120=1570; export (RS nélkül) (700+300+200+150+100)/4=362.5
print("várt net 13:00Z =", 1570 - 362.5)
# RS export hiányzik -> nem lehet 'teljes' óra, ha RS aktív:  ellenőrizzük
print("RS (csak import) aktív:", "RS" in [b["code"] for b in d["borders"] if b["last_ts"]], "| complete bármi:", any(d["complete"]))
assert abs(H["2026-11-23T13:00:00Z"][0]-1207.5)<1e-6 and H["2026-11-23T13:00:00Z"][1] is True
assert d["last_complete_ts"]=="2026-11-23T13:00:00Z"
assert H["2026-11-23T14:00:00Z"][1] is False
assert "UA" not in d["prices"]["zones"] and "HU" in d["prices"]["zones"]
print("ÁR-ELLENŐRZÉS: HU =", d["prices"]["zones"]["HU"][0], "AT =", d["prices"]["zones"]["AT"][0], "(várt 102.0 / 102.0)")
bd = {b["code"]: b for b in d["borders"]}
print("last_ts:", {k: v["last_ts"] for k, v in bd.items()})
print("ár zónák:", list(d["prices"]["zones"]), "| pont:", len(d["prices"]["ts"]), "| első/utolsó:", d["prices"]["ts"][0], d["prices"]["ts"][-1])
print("JSON méret:", len(js), "bájt")
