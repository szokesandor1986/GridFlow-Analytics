"""A history.py tesztje kitalált, több évre visszanyúló klienssel, hálózat nélkül.  Futtatás:  python -I tests/test_history.py"""
import importlib.util
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
cspec = importlib.util.spec_from_file_location("collector", ROOT / "collector.py")
col = importlib.util.module_from_spec(cspec)
cspec.loader.exec_module(col)
sys.modules["collector"] = col                      # a history.py ezt importálja
hspec = importlib.util.spec_from_file_location("history", ROOT / "history.py")
hist = importlib.util.module_from_spec(hspec)
hspec.loader.exec_module(hist)
from fake_history_client import FakeHistoryClient  # noqa: E402

NOW = pd.Timestamp("2026-11-23 14:20", tz="UTC")


def test_history():
    c = FakeHistoryClient(NOW)
    h = hist.collect_history(c, now=NOW, from_year=2024, workers=4)
    m = h["months"]
    assert "2024-01" in m and "2026-11" in m and m["2026-11"]["partial"] and not m["2026-07"]["partial"], sorted(m)[:3]
    assert h["years_done"] == [2024, 2025], h["years_done"]
    # a modell trendjei: a nap részaránya és a negatív órák száma nő
    assert m["2026-07"]["gen"]["solar"] > m["2024-07"]["gen"]["solar"] > 0
    assert m["2026-07"]["price"]["neg_h"] > m["2024-07"]["price"]["neg_h"]
    assert 0.9 < m["2026-07"]["z"]["AT"]["corr"] <= 1.0
    assert "RS" not in m["2026-07"]["z"] and "UA" not in m["2026-07"]["border"]
    ldc = h["annual"]["2025"]["ldc"]
    assert len(ldc) == 21 and ldc[0] > ldc[-1] and all(a >= b for a, b in zip(ldc, ldc[1:]))
    assert h["annual"]["2026"]["inst"]["solar"] > 1000
    json.dumps(h, allow_nan=False)
    print(f"1. havi összesítés rendben ({len(m)} hónap)")

    # második futás: csak a folyó évet tölti le újra
    c2 = FakeHistoryClient(NOW)
    h2 = hist.collect_history(c2, now=NOW, from_year=2024, existing=json.loads(json.dumps(h)), workers=4)
    assert {y for _, y in c2.calls} == {2026} and len(h2["months"]) == len(m)
    print("2. inkrementális frissítés rendben")


def test_broken_year():
    class Broken(FakeHistoryClient):
        def query_load(self, zone, start, end, **kw):
            if start.year == 2025:
                raise ConnectionError("x")
            return super().query_load(zone, start, end, **kw)

        def query_generation(self, zone, start, end, **kw):
            if start.year == 2025:
                raise ConnectionError("x")
            return super().query_generation(zone, start, end, **kw)
    h = hist.collect_history(Broken(NOW), now=NOW, from_year=2024, workers=4)
    assert 2025 not in h["years_done"] and 2024 in h["years_done"] and "2024-05" in h["months"], h["years_done"]
    assert any("2025" in e for e in h["errors"]), h["errors"]
    print("3. hibás év nem rontja el a többit")


if __name__ == "__main__":
    col.time.sleep = lambda s: None
    test_history()
    test_broken_year()
    print("MINDEN TESZT ÁTMENT")
