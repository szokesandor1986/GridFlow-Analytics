# Határkeresztező villamosenergia-áramlás – Magyarország

Statikus oldal, amely az ENTSO-E Transparency Platform adataiból mutatja:

- a magyar határokon át mért fizikai áramlást (AT, SK, UA, RO, RS, HR, SI), határonként és nettó import/export bontásban,
- a day-ahead zónaárakat (HU és a szomszédos zónák),
- az elmúlt 48 óra nettó importját és a három napos (tegnap, ma, holnap) árgörbét.

Egy Python szkript (`collector.py`) időnként lekéri az adatot és `data.json`-ba írja, az `index.html` ezt olvassa. Az ENTSO-E token csak a szkriptben, a GitHub titkai között él, a böngészőbe nem kerül.

## Beüzemelés GitHub Pages-en

1. **Token.** Az ENTSO-E fiókodban (My Account → Web API Security Token) generálj tokent. Ha nincs ilyen menüpont, a RESTful API hozzáférést a transparency@entsoe.eu címen kell kérni.
2. **Repó.** Hozz létre egy GitHub-repót, és töltsd fel ennek a mappának a tartalmát (a rejtett `.github` mappával együtt). A Pages ingyenesen nyilvános repóval működik.
3. **Titok.** Settings → Secrets and variables → Actions → New repository secret. Név: `ENTSOE_TOKEN`, érték: a token.
4. **Pages.** Settings → Pages → Source: *GitHub Actions*.
5. **Első futtatás.** Actions → „Adatfrissítés és közzététel" → Run workflow. Sikeres futás után az oldal a `https://<felhasználó>.github.io/<repó>/` címen elérhető, utána 20 percenként frissül.

A workflow nem commitol, minden futás új közzétételt készít, így a repó előzménye nem hízik.

## Beüzemelés saját szerveren (GitHub helyett)

Debian vagy Ubuntu alapú gépre (bérelt VPS, vagy Raspberry Pi OS) való. Az 1–2 magos, 1 GB memóriás gép bőven elég.

1. Másold fel a projekt mappáját a szerverre (például `scp -r hu-hatar-aramlas felhasznalo@szerver:`).
2. Futtasd rootként: `sudo ./deploy/install.sh` (IP-címen, HTTP-n), vagy `sudo ./deploy/install.sh adat.pelda.hu` (saját tartománnyal, HTTPS-sel). Tartománynál az A rekord előbb mutasson a szerverre, és a 80-as meg a 443-as port legyen nyitva.
3. A telepítő bekéri az ENTSO-E tokent (nem látszik a képernyőn), `600` jogosultsággal az `/etc/hu-aramlas.env` fájlba menti, majd beállítja a Caddy webszervert és egy systemd időzítőt, ami 15 percenként lefuttatja a gyűjtőt.
4. Ellenőrzés: `systemctl list-timers hu-aramlas.timer`, napló: `journalctl -u hu-aramlas.service -n 50`.

Otthoni gépnél a tartományos HTTPS-hez port-továbbítás kell a routeren; ha ezt nem szeretnéd, a Cloudflare Tunnel használható helyette. Ez a mappa nem tartalmazza a beállítását.

## Beüzemelés webtárhelyen (Rackhost és hasonlók)

Ez a legegyszerűbb út, ha már van vagy venni akarsz egy sima webtárhelyet. A tárhelyen nem kell Python, cron vagy SSH: csak az `index.html`, a `demo-data.json` és a `data.json` kerül rá. Az adatot továbbra is a GitHub gyűjti, és minden futás végén FTPS-en feltölti.

1. Hozz létre egy GitHub-repót a projekttel, és vedd fel a titkot: `ENTSOE_TOKEN` (lásd fent).
2. Kérdezd meg a szolgáltatótól, vagy nézd meg a tárhely adminisztrációs felületén: mi az FTP-szerver címe, van-e **FTPS** (FTP TLS-sel) támogatás, és melyik mappából szolgálja ki a weboldalt (gyakran `public_html` vagy `www`). Egy külön FTP-felhasználót érdemes erre a célra létrehozni.
3. Vedd fel a titkokat: `FTP_HOST`, `FTP_USER`, `FTP_PASS`, és ha kell, `FTP_DIR` (pl. `/public_html`).
4. Másold a `deploy/github-actions-tarhely.yml.example` fájlt a `.github/workflows/update.yml` helyére (felülírva az eredetit), majd az Actions fülön indítsd el kézzel a workflow-t.

A feltöltő szkript (`deploy/upload_ftps.py`) kötelezően titkosítva és ellenőrzött tanúsítvánnyal csatlakozik. Ha a tárhely csak sima FTP-t tud, a szkript hibával leáll, és a jelszó nem megy ki titkosítatlanul. Ilyenkor kérj FTPS-t vagy válts szolgáltatót. A szkriptet helyi, TLS-es teszt FTP-szerverrel próbáltam ki (feltöltés, felülírás, rossz jelszó, nem ellenőrizhető tanúsítvány, hiányzó mappa), valódi szolgáltatóval nem.

Ha a tárhelyen van SSH, cron és Python 3, a gyűjtő közvetlenül ott is futhat: `pip install --user -r requirements.txt`, majd egy 15–20 percenként futó cron-feladat: `ENTSOE_TOKEN=... python collector.py --out /elérési/út/data.json`. Ez a változat nincs kipróbálva.

## Kipróbálás a gépeden

```
pip install -r requirements.txt
export ENTSOE_TOKEN="a-te-tokened"
python collector.py --out data.json
python -m http.server
```

Ezután a `http://localhost:8000` címen az éles adat látszik. A `http://localhost:8000/?demo=1` cím a mellékelt, szintetikus `demo-data.json` fájlt tölti be, piros „DEMO ADAT" sávval. Ez csak a kinézet előnézete, nem valós mérés.

A `tests/` mappa tesztje kitalált klienssel ellenőrzi a gyűjtő számítását (hálózat nélkül): `python -I tests/test_collector.py`.

## Mit érdemes tudni az adatról

- **Nem valós idejű.** A fizikai áramlást az ENTSO-E a rendszerirányítók közlése után teszi közzé, jellemzően órás felbontásban és határonként eltérő késéssel. A fő szám az utolsó olyan óra, amelyre minden elérhető határ adata megvan, az értékek órás átlagok (MW).
- **Előjel.** Pozitív érték import (a szomszédból Magyarországra), negatív export. Egy határ nettó értéke az import és az export különbsége.
- **Az ár day-ahead ár.** A másnapi piac zónaára, nem a pillanatnyi, az intraday vagy a kiegyenlítő energia ára. Az SDAC 2025. október 1. óta 15 perces piaci időegységgel dolgozik, az oldal a forrás felbontását követi.
- **Hiányzó adat.** Egyes határokra vagy zónákra az ENTSO-E nem mindig közöl adatot (jellemzően UA, ritkábban RS ára). Ilyenkor az oldal ezt kiírja, nem pótol és nem becsül.
- **MAVIR.** A MAVIR saját adatait a mavir.hu adatközlő oldala publikálja. Hivatalos, nyilvános API-t nem találtam hozzá, ezért ez az oldal az ENTSO-E-re épül. A MAVIR saját közlése eltérhet ettől.
- **Elavulás.** Ha az adatgyűjtés három óránál régebben futott le, az oldal sárga figyelmeztetést mutat. Sikertelen futásnál az előző közzététel marad élő.
- **Ütemezés leállása.** A GitHub nyilvános repóban 60 nap repóaktivitás nélkül automatikusan kikapcsolja az ütemezett workflow-kat. Ilyenkor az Actions fülön egy kattintással újra lehet engedélyezni, vagy egy új commit is elég.
- **Forrásmegjelölés.** Az oldal láblécében szerepel az ENTSO-E Transparency Platform mint forrás. Nyilvános közzététel előtt érdemes elolvasni az ENTSO-E felhasználási feltételeit.
