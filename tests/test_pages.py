"""Böngészős oldalteszt (Playwright): mind az öt oldal a mellékelt demo adattal.

Mit ellenőriz:
  - egyik oldalon sincs JavaScript-hiba vagy konzolhiba (asztali és mobil nézetben, világos és sötét módban),
  - nincs vízszintes túlcsordulás (a lap nem szélesebb a képernyőnél),
  - a fő tartalom megjelent (cím, kártyák, ábrák),
  - a navigációban mind az öt fül ott van, és mindegyik létező oldalra mutat,
  - Európai piac: az óránkénti csúszka húzása közben a mutatógombok nem tűnnek el,
  - Rendszerállapot: a távoli országok választója és az ország-sáv nem takarja ki az oldalt,
  - Időjárás: legfeljebb négy település kapcsolható be, az ábrák a választással együtt frissülnek.

Futtatás (a projekt mappájából):  python -I tests/test_pages.py
Kell hozzá:  pip install playwright && playwright install chromium
(vagy egy telepített Chrome: CHROME_PATH=/usr/bin/google-chrome). Ha a Playwright nincs
telepítve, a teszt kihagyja magát (kilépési kód 0), így a gyűjtő workflow-t nem akasztja meg.
"""
import contextlib
import functools
import http.server
import os
import socketserver
import sys
import threading

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("KIHAGYVA: a Playwright nincs telepítve (pip install playwright)")
    sys.exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["index", "muszaki", "europa", "idojaras", "foldgaz"]
FAIL = []


def check(ok, msg):
    print(("  ok   " if ok else "  HIBA ") + msg)
    if not ok:
        FAIL.append(msg)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@contextlib.contextmanager
def serve():
    handler = functools.partial(Quiet, directory=ROOT)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()


def launch(pw):
    path = os.environ.get("CHROME_PATH")
    if not path and not os.environ.get("PLAYWRIGHT_BUNDLED"):
        for c in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if os.path.exists(c):
                path = c
                break
    kw = {"args": ["--no-sandbox"]}
    if path:
        kw["executable_path"] = path
    return pw.chromium.launch(**kw)


def open_page(browser, base, name, vp, scheme, errs, wait=3500):
    pg = browser.new_page(viewport=vp, color_scheme=scheme)
    pg.on("pageerror", lambda e: errs.append("JS: " + str(e)[:160]))
    pg.on("console", lambda m: errs.append("konzol: " + m.text[:160]) if m.type == "error" else None)
    pg.goto("%s/%s.html?demo=1" % (base, name))
    pg.wait_for_timeout(wait)
    return pg


def overflow(pg):
    return pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def test_all_pages(browser, base):
    for name in PAGES:
        for label, vp in (("asztali", {"width": 1280, "height": 900}), ("mobil", {"width": 390, "height": 800})):
            for scheme in ("light", "dark"):
                errs = []
                pg = open_page(browser, base, name, vp, scheme, errs)
                n_card = pg.evaluate("document.querySelectorAll('.card').length")
                n_svg = pg.evaluate("document.querySelectorAll('svg').length")
                nav = pg.evaluate("[...document.querySelectorAll('nav a')].map(a=>a.getAttribute('href')).filter(h=>h&&!h.startsWith('#'))")
                ov = overflow(pg)
                tag = "%s / %s / %s" % (name, label, scheme)
                check(not errs, "%s: nincs JS- vagy konzolhiba %s" % (tag, errs[:2]))
                check(ov <= 0, "%s: nincs vízszintes túlcsordulás (%s px)" % (tag, ov))
                check(n_card >= 2 and n_svg >= 1, "%s: tartalom megjelent (kártya %d, ábra %d)" % (tag, n_card, n_svg))
                if scheme == "light" and label == "asztali":
                    check(len(nav) == 5 and all(os.path.exists(os.path.join(ROOT, h.split("?")[0] or "index.html")) for h in nav),
                          "%s: öt fül, mind létező oldalra mutat %s" % (name, nav))
                pg.close()


def test_europa_slider(browser, base):
    errs = []
    pg = open_page(browser, base, "europa", {"width": 1200, "height": 900}, "light", errs, wait=5000)
    pg.click("#mapTime button:has-text('Óránként')")
    pg.wait_for_timeout(300)
    for metric in ("CO₂-intenzitás", "Megújuló részarány", "Nettó import / export"):
        pg.click("#mapChips .chip:has-text('%s')" % metric)
        pg.wait_for_timeout(250)
        mn, mx = pg.evaluate("[+document.querySelector('#mapTime input').min, +document.querySelector('#mapTime input').max]")
        seen = set()
        for v in range(mn, mx + 1, 8):
            pg.evaluate("v=>{const s=document.querySelector('#mapTime input'); s.value=v; s.dispatchEvent(new Event('input'))}", v)
            pg.wait_for_timeout(100)
            state = pg.evaluate("[document.querySelectorAll('#mapChips .chip').length, document.querySelector('#mapChips .chip[aria-pressed=true]')?.textContent, !!document.querySelector('#mapTime input')]")
            seen.add(repr(state))
        check(len(seen) == 1, "Európai piac / %s: húzás közben a gombok és a kiválasztott mutató nem változik %s" % (metric, sorted(seen)))
    check(not errs, "Európai piac csúszka: nincs JS-hiba %s" % errs[:2])
    pg.close()


def test_muszaki_country_bar(browser, base):
    errs = []
    bar = "(document.querySelector('#ctl')||document.querySelector('.ctl'))"
    pg = open_page(browser, base, "muszaki", {"width": 390, "height": 800}, "light", errs, wait=4000)
    check(pg.query_selector(".zsel") is not None, "Rendszerállapot / mobil: van ország-választó lenyíló")
    h = pg.evaluate(bar + ".getBoundingClientRect().height")
    check(h < 400, "Rendszerállapot / mobil: a vezérlősáv nem takarja ki az oldalt (%d px)" % h)
    pg.close()
    pg = open_page(browser, base, "muszaki", {"width": 1280, "height": 900}, "light", errs, wait=4000)
    h = pg.evaluate(bar + ".getBoundingClientRect().height")
    check(h < 200, "Rendszerállapot / asztali: a vezérlősáv kompakt marad (%d px)" % h)
    check(not errs, "Rendszerállapot: nincs JS-hiba %s" % errs[:2])
    pg.close()


def test_idojaras(browser, base):
    errs = []
    pg = open_page(browser, base, "idojaras", {"width": 1280, "height": 900}, "light", errs, wait=1500)
    for n in ("Budapest", "Győr", "Pécs", "Szeged", "Bécs"):
        pg.click("#placeChips button:has-text('%s')" % n)
    on = pg.evaluate("[...document.querySelectorAll('#placeChips button[aria-pressed=true]')].length")
    check(on == 1 + 4, "Időjárás: legfeljebb négy település kapcsolható be (magyar átlag + %d)" % (on - 1))
    n_paths = pg.evaluate("document.querySelectorAll('#tempChart svg path').length")
    check(n_paths >= 5, "Időjárás: a hőmérséklet-ábrán az átlag és a négy hely vonala látszik (%d)" % n_paths)
    pg.click("#winChips button:has-text('±1 nap')")
    pg.wait_for_timeout(250)
    check(pg.evaluate("document.querySelector('#winChips button[aria-pressed=true]').textContent") == "±1 nap",
          "Időjárás: az időablak-gomb átvált")
    pg.focus("#tempChart svg")
    pg.keyboard.press("ArrowLeft")
    tip = pg.evaluate("document.querySelector('#tempChart .tip').innerText")
    check("°C" in tip, "Időjárás: billentyűzettel bejárható az ábra, a tooltip mértékegységet ír")
    check(pg.evaluate("document.querySelectorAll('#placeTable tbody tr').length") >= 10, "Időjárás: a helyek táblázata kitöltött")
    foot = pg.evaluate("document.getElementById('foot').innerText")
    check("Open-Meteo" in foot and "CC BY 4.0" in foot, "Időjárás: a forrásmegjelölés (Open-Meteo, CC BY 4.0) a lábléc része")
    check(not errs, "Időjárás: nincs JS-hiba %s" % errs[:2])
    pg.close()


def test_missing_data(browser, base):
    """Éles mód adatfájl nélkül (nincs weather.json a mappában): érthető üzenet, nincs kezeletlen hiba."""
    if os.path.exists(os.path.join(ROOT, "weather.json")):
        print("  (kihagyva: a mappában van weather.json)")
        return
    errs = []
    pg = browser.new_page(viewport={"width": 1000, "height": 800})
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto(base + "/idojaras.html")
    pg.wait_for_timeout(1500)
    txt = pg.evaluate("document.getElementById('cNow').innerText")
    check("nem tölthető be" in txt, "Időjárás: hiányzó adatfájlnál érthető üzenet jelenik meg")
    check(not errs, "Időjárás: hiányzó adatfájlnál nincs kezeletlen JS-hiba %s" % errs[:2])
    pg.close()


def main():
    with serve() as base, sync_playwright() as pw:
        browser = launch(pw)
        try:
            for fn in (test_all_pages, test_europa_slider, test_muszaki_country_bar, test_idojaras, test_missing_data):
                print(fn.__name__)
                fn(browser, base)
        finally:
            browser.close()
    print("\n%s" % ("MINDEN OK" if not FAIL else "%d HIBA:\n  - " % len(FAIL) + "\n  - ".join(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
