"""Kitalált AGSI+ / ENTSOG kliens a gas.py tesztjéhez és a DEMO adathoz. NEM valós mérés.

Ugyanazt a get(url, params, headers, timeout) felületet adja, mint a gas.Http, és ugyanolyan alakú választ
(idézőjeles számok az AGSI+-nál, "operationaldatas" lista az ENTSOG-nál, kWh/d egység).
A pontkulcsok kitaláltak (DEMO-...), a valódiakat a gas.py futáskor az /interconnections végpontról deríti fel.
"""
import math
from datetime import date, datetime, timedelta

CAP = {  # munkagáz-kapacitás TWh, csúcs-töltöttség %, tél végi mélypont %
    "HU": (67.0, 92, 30), "AT": (95.0, 90, 25), "SK": (36.0, 93, 35), "RO": (31.0, 88, 22),
    "HR": (6.0, 85, 20), "UA": (310.0, 70, 18), "DE": (250.0, 94, 25), "IT": (194.0, 95, 30),
    "CZ": (44.0, 92, 28), "PL": (36.0, 98, 40), "BG": (6.0, 90, 20), "NL": (140.0, 96, 22),
    "FR": (130.0, 95, 28), "EU": (1130.0, 93, 28),
}


def fill_pct(code, d, bias=0.0):
    """Szezonális görbe: kb. nov. 1-jén csúcs, kb. márc. 31-én mélypont (gázév: okt. 1 – szept. 30).
    Az évenkénti eltérés (bias) a gázév két végén nullára simul, így nincs ugrás a gázévek határán."""
    hi, lo = CAP[code][1], CAP[code][2]
    gy_start = date(d.year if d.month >= 10 else d.year - 1, 10, 1)
    t = (d - gy_start).days
    if t < 31:
        x = hi - 10 * (31 - t) / 31
    elif t < 182:
        x = hi - (hi - lo) * ((t - 31) / 151) ** 1.1
    else:
        x = lo + (hi - lo - 10) * min(1, (t - 182) / 183) ** 0.9
    x += bias * math.sin(math.pi * min(t, 364) / 365)
    return max(3.0, min(99.5, x + 1.5 * math.sin(d.toordinal() / 9.0)))


def agsi_record(code, d, today):
    cap = CAP[code][0]
    gy = d.year if d.month >= 10 else d.year - 1
    yr_bias = ((gy * 7 + (1 if code == "HU" else 3)) % 9) - 4
    f = fill_pct(code, d, yr_bias)
    f_prev = fill_pct(code, d - timedelta(days=1), yr_bias)
    gis = cap * f / 100
    net_gwh = (f_prev - f) / 100 * cap * 1000          # pozitív = kivétel
    inj, wdr = max(0.0, -net_gwh), max(0.0, net_gwh)
    scale = 1.0 / 300 if code != "EU" else 1.0
    rec = {
        "name": COUNTRY(code), "code": code, "url": code.lower(), "gasDayStart": d.isoformat(),
        "gasInStorage": f"{gis:.4f}", "consumption": "n/a", "consumptionFull": "n/a",
        "injection": f"{inj:.2f}", "withdrawal": f"{wdr:.2f}", "netWithdrawal": f"{wdr - inj:.2f}",
        "workingGasVolume": f"{cap:.2f}", "injectionCapacity": f"{cap * 1000 * 0.0045:.2f}",
        "withdrawalCapacity": f"{cap * 1000 * 0.0075:.2f}", "status": "E",
        "trend": f"{(inj - wdr) / (cap * 1000) * 100:.3f}", "full": f"{f:.2f}",
        "info": [], "updatedAt": f"{(today).isoformat()} 19:41:00",
    }
    if code == "HU" and d == today - timedelta(days=1):
        rec["children"] = [
            {"name": "MFGT (DEMO)", "code": "21XDEMO000000001", "gasInStorage": f"{gis * 0.62:.3f}", "workingGasVolume": f"{cap * 0.62:.2f}",
             "full": f"{f + 1:.2f}", "injection": f"{inj * 0.62:.1f}", "withdrawal": f"{wdr * 0.62:.1f}",
             "injectionCapacity": f"{cap * 4.5 * 0.62:.1f}", "withdrawalCapacity": f"{cap * 7.5 * 0.62:.1f}", "status": "E"},
            {"name": "HEXUM (DEMO)", "code": "21XDEMO000000002", "gasInStorage": f"{gis * 0.38:.3f}", "workingGasVolume": f"{cap * 0.38:.2f}",
             "full": f"{f - 1:.2f}", "injection": f"{inj * 0.38:.1f}", "withdrawal": f"{wdr * 0.38:.1f}",
             "injectionCapacity": f"{cap * 4.5 * 0.38:.1f}", "withdrawalCapacity": f"{cap * 7.5 * 0.38:.1f}", "status": "E"},
        ]
    return rec


def COUNTRY(code):
    return {"EU": "Europe"}.get(code, code)


POINTS = [  # (kulcs, címke, szomszéd, hu-oldali irány, alap GWh/nap, téli többlet)
    ("DEMO-PT-AT", "Mosonmagyaróvár (DEMO)", "AT", "entry", 40, 30),
    ("DEMO-PT-SK", "Balassagyarmat (DEMO)", "SK", "exit", 25, 30),
    ("DEMO-PT-RO", "Csanádpalota (DEMO)", "RO", "exit", 12, 14),
    ("DEMO-PT-HR", "Drávaszerdahely (DEMO)", "HR", "exit", 10, 12),
    ("DEMO-PT-RS", "Kiskundorozsma (DEMO)", "RS", "entry", 120, 70),
    ("DEMO-PT-UA", "VIP Bereg (DEMO)", "UA", "exit", 12, 22),
]


def winter(d):
    return 0.5 + 0.5 * math.cos((d.timetuple().tm_yday - 20) / 365.0 * 2 * math.pi)


class FakeHttp:
    def __init__(self, today, key="demo", fail_entsog=False, fail_agsi_country=(), no_opd=False):
        self.today, self.key = today, key
        self.fail_entsog, self.fail_agsi_country = fail_entsog, set(fail_agsi_country)
        self.no_opd = no_opd
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=60):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "agsi" in url:
            return self._agsi(params, headers or {})
        if "operatorpointdirections" in url:
            if self.no_opd:
                raise RuntimeError("HTTP 500")
            return self._opd()
        if "interconnections" in url:
            return self._inter()
        return self._flows(params)

    # ---- AGSI+
    def _agsi(self, p, headers):
        if headers.get("x-key") != self.key:
            e = RuntimeError("HTTP 403")
            e.status_code = 403
            raise e
        code = "EU" if p.get("type") == "eu" else p.get("country")
        if code in self.fail_agsi_country:
            raise RuntimeError("HTTP 500")
        if code not in CAP:
            return {"last_page": 1, "total": 0, "dataset": "x", "gas_day": "", "data": []}
        a = datetime.strptime(p["from"], "%Y-%m-%d").date()
        b = min(datetime.strptime(p["to"], "%Y-%m-%d").date(), self.today - timedelta(days=1))
        days = [b - timedelta(days=i) for i in range((b - a).days + 1)]      # az AGSI+ alapból csökkenő sorrend
        size, page = int(p.get("size", 30)), int(p.get("page", 1))
        chunk = days[(page - 1) * size: page * size]
        return {"last_page": max(1, math.ceil(len(days) / size)), "total": len(days), "dataset": "demo",
                "gas_day": b.isoformat(), "data": [agsi_record(code, d, self.today) for d in chunk]}

    # ---- ENTSOG
    def _opd(self):
        recs = []
        for key, label, nb, side, *_ in POINTS:
            recs.append({"operatorKey": "HU-TSO-0001", "pointKey": key, "pointLabel": label, "directionKey": side,
                         "tSOCountry": "HU", "adjacentCountry": nb})
            recs.append({"operatorKey": f"{nb}-TSO-DEMO", "pointKey": key, "pointLabel": label,
                         "directionKey": "exit" if side == "entry" else "entry", "tSOCountry": nb, "adjacentCountry": "HU"})
        recs.append({"operatorKey": "HU-TSO-0001", "pointKey": "DEMO-UGS", "pointLabel": "Belső tároló", "directionKey": "entry",
                     "tSOCountry": "HU", "adjacentCountry": ""})
        return {"operatorpointdirections": recs}

    def _inter(self):
        recs = []
        for key, label, nb, side, *_ in POINTS:
            if side == "entry":      # a szomszéd -> HU
                recs.append({"pointKey": key, "pointLabel": label, "fromCountryKey": nb, "toCountryKey": "HU",
                             "fromOperatorKey": f"{nb}-TSO-DEMO", "toOperatorKey": "HU-TSO-0001"})
            else:
                recs.append({"pointKey": key, "pointLabel": label, "fromCountryKey": "HU", "toCountryKey": nb,
                             "fromOperatorKey": "HU-TSO-0001", "toOperatorKey": f"{nb}-TSO-DEMO"})
        recs.append({"pointKey": "DEMO-OTHER", "pointLabel": "Nem magyar pont", "fromCountryKey": "AT", "toCountryKey": "DE",
                     "fromOperatorKey": "AT-TSO-X", "toOperatorKey": "DE-TSO-X"})
        return {"interconnections": recs}

    def _flows(self, p):
        if self.fail_entsog:
            raise RuntimeError("HTTP 500")
        a = datetime.strptime(p["from"], "%Y-%m-%d").date()
        b = datetime.strptime(p["to"], "%Y-%m-%d").date()
        wanted = [x for x in p["pointDirection"].split(",") if x]
        out = []
        for key, label, nb, side, base, extra in POINTS:
            if f"HU-TSO-0001{key}{side}" not in wanted:
                continue
            for i in range((b - a).days + 1):
                d = a + timedelta(days=i)
                v = base + extra * winter(d) + 4 * math.sin(d.toordinal() / 3.0 + len(key))
                out.append({"indicator": "Physical Flow", "periodType": "day", "periodFrom": f"{d.isoformat()}T06:00:00+01:00",
                            "periodTo": f"{(d + timedelta(days=1)).isoformat()}T06:00:00+01:00", "operatorKey": "HU-TSO-0001",
                            "pointKey": key, "pointLabel": label, "directionKey": side, "unit": "kWh/d",
                            "value": round(max(0.0, v) * 1e6)})
        return {"meta": {"count": len(out)}, "operationaldatas": out}
