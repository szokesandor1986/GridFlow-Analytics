#!/usr/bin/env python3
"""ENTSO-E adatgyűjtő a magyar határkeresztező villamosenergia-áramlás oldalhoz.

Lekéri (ENTSO-E Transparency Platform, RESTful API):
  - a HU és a szomszédos zónák közötti fizikai áramlást (MW), mindkét irányban,
  - a HU és a szomszédos zónák day-ahead árát (EUR/MWh),
és egy data.json fájlba írja, amit az index.html olvas.

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
            if exc.__class__.__name__ in ("NoMatchingDataError", "InvalidBusinessParameterError"):
                raise
            last = exc
            if attempt < tries:
                time.sleep(wait * attempt)
    raise last


def to_list(series):
    """Pandas sorozat -> JSON-barát lista (NaN -> None), 2 tizedesre kerekítve."""
    return [None if pd.isna(v) else round(float(v), 2) for v in series]


def iso(index):
    return [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in index]


def hourly_mean(s):
    return s.tz_convert("UTC").resample("h").mean()


def collect(client, now=None, hours=48):
    now = now or pd.Timestamp.now(tz="UTC")
    start = (now - pd.Timedelta(hours=hours)).floor("h")
    end = now.ceil("h") + pd.Timedelta(hours=1)
    errors = []

    # ---- fizikai áramlás
    imp, exp = {}, {}
    for code, name in NEIGHBOURS.items():
        for direction, store, a, b in (("import", imp, code, HOME), ("export", exp, HOME, code)):
            try:
                s = with_retry(lambda a=a, b=b: client.query_crossborder_flows(a, b, start=start, end=end))
                if len(s):
                    store[code] = hourly_mean(s)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"áramlás {code} {direction}: {exc.__class__.__name__}")

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
    local_midnight = now.tz_convert("Europe/Budapest").normalize()
    p_start = local_midnight - pd.Timedelta(days=1)
    p_end = local_midnight + pd.Timedelta(days=2)
    prices = {}
    for zone in PRICE_ZONES:
        try:
            s = with_retry(lambda zone=zone: client.query_day_ahead_prices(zone, start=p_start, end=p_end))
            if len(s):
                prices[zone] = s.tz_convert("UTC")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ár {zone}: {exc.__class__.__name__}")

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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data.json")
    p.add_argument("--hours", type=int, default=48, help="ennyi órára visszamenőleg kéri az áramlást")
    args = p.parse_args()

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        sys.exit("Hiányzik az ENTSOE_TOKEN környezeti változó.")

    from entsoe import EntsoePandasClient

    data = collect(EntsoePandasClient(api_key=token), hours=args.hours)
    if not any(b["last_ts"] for b in data["borders"]):
        sys.exit("Egyik határra sem érkezett áramlási adat; a data.json nem íródik felül.")

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, args.out)
    ok = [b["code"] for b in data["borders"] if b["last_ts"]]
    print(f"Kész: {args.out} | határok adattal: {ok} | hibák: {len(data['errors'])}")
    for e in data["errors"]:
        print("  -", e)


if __name__ == "__main__":
    main()
