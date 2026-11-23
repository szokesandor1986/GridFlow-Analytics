"""Szintetikus DEMO adat az oldalak előnézetéhez. NEM valós mérés.

A collector.py saját függvényein át készül, egy kitalált ENTSO-E klienssel (tests/fake_client.py),
ezért a séma pontosan megegyezik az éles fájlokéval. A kimenetben "demo": true.

Használat:  python -I tests/make_demo.py            (a projekt mappájába: demo-data.json, demo-engineer.json)
"""
import importlib.util
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
spec = importlib.util.spec_from_file_location("collector", ROOT / "collector.py")
col = importlib.util.module_from_spec(spec)
spec.loader.exec_module(col)
from fake_client import FakeClient  # noqa: E402

NOW = pd.Timestamp("2026-11-23 13:20", tz="Europe/Budapest").tz_convert("UTC")
client = FakeClient(NOW)

main = col.collect(client, now=NOW)
main["demo"] = True
main["source"] = "DEMO – szintetikus adat, nem valós mérés"
col.write_json(str(ROOT / "demo-data.json"), main)

eng = col.collect_engineer(client, now=NOW, main=main)
eng["demo"] = True
eng["source"] = "DEMO – szintetikus adat, nem valós mérés"
col.write_json(str(ROOT / "demo-engineer.json"), eng)

eu = col.collect_europe(client, now=NOW)
eu["demo"] = True
eu["source"] = "DEMO – szintetikus adat, nem valós mérés"
col.write_json(str(ROOT / "demo-europe.json"), eu)

sys.modules["collector"] = col
hspec = importlib.util.spec_from_file_location("history", ROOT / "history.py")
hist = importlib.util.module_from_spec(hspec)
hspec.loader.exec_module(hist)
from fake_history_client import FakeHistoryClient  # noqa: E402
h = hist.collect_history(FakeHistoryClient(NOW), now=NOW, from_year=2019, workers=8)
h["demo"] = True
h["source"] = "DEMO – szintetikus adat, nem valós mérés"
col.write_json(str(ROOT / "demo-history.json"), h)
print("europe zónák:", sorted(eu["zones"]), "| history hónapok:", len(h["months"]))

print("ok | utolsó teljes óra:", main["last_complete_ts"], "| fő hibák:", main["errors"], "| mérnöki hibák:", eng["errors"])
print("modulok:", {k: v for k, v in eng["modules"].items()})

# gáz fül: kitalált AGSI+/ENTSOG kliens
from datetime import date as _date  # noqa: E402
gspec = importlib.util.spec_from_file_location("gas", ROOT / "gas.py")
gas = importlib.util.module_from_spec(gspec)
gspec.loader.exec_module(gas)
from fake_gas_client import FakeHttp  # noqa: E402
g = gas.collect(FakeHttp(_date(2026, 11, 23)), "demo", None, today=_date(2026, 11, 23), throttle=gas.Throttle(gap=0))
g["demo"] = True
g["source"] = "DEMO – szintetikus adat, nem valós mérés"
(ROOT / "demo-gas.json").write_text(json.dumps(g, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("gáz: napok", len(g["agsi"]["dates"]), "| pontok", len(g["flows"]["points"]), "| hibák", g["errors"])
