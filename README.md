# Határkeresztező villamosenergia-áramlás – Magyarország

Statikus oldal, amely az ENTSO-E Transparency Platform adataiból mutatja:

- a magyar határokon át mért fizikai áramlást (AT, SK, UA, RO, RS, HR, SI), határonként és nettó import/export bontásban,
- a day-ahead zónaárakat (HU és a szomszédos zónák),
- az elmúlt 48 óra nettó importját és a három napos (tegnap, ma, holnap) árgörbét.



## Mit érdemes tudni az adatról

- **Nem valós idejű.** A fizikai áramlást az ENTSO-E a rendszerirányítók közlése után teszi közzé, jellemzően órás felbontásban és határonként eltérő késéssel. A fő szám az utolsó olyan óra, amelyre minden elérhető határ adata megvan, az értékek órás átlagok (MW).
- **Előjel.** Pozitív érték import (a szomszédból Magyarországra), negatív export. Egy határ nettó értéke az import és az export különbsége.
- **Az ár day-ahead ár.** A másnapi piac zónaára, nem a pillanatnyi, az intraday vagy a kiegyenlítő energia ára. Az SDAC 2025. október 1. óta 15 perces piaci időegységgel dolgozik, az oldal a forrás felbontását követi.
- **Hiányzó adat.** Egyes határokra vagy zónákra az ENTSO-E nem mindig közöl adatot (jellemzően UA, ritkábban RS ára). Ilyenkor az oldal ezt kiírja, nem pótol és nem becsül.
- **MAVIR.** A MAVIR saját adatait a mavir.hu adatközlő oldala publikálja. Hivatalos, nyilvános API-t nem találtam hozzá, ezért ez az oldal az ENTSO-E-re épül. A MAVIR saját közlése eltérhet ettől.
- **Elavulás.** Ha az adatgyűjtés három óránál régebben futott le, az oldal sárga figyelmeztetést mutat. Sikertelen futásnál az előző közzététel marad élő.
- **Ütemezés leállása.** A GitHub nyilvános repóban 60 nap repóaktivitás nélkül automatikusan kikapcsolja az ütemezett workflow-kat. Ilyenkor az Actions fülön egy kattintással újra lehet engedélyezni, vagy egy új commit is elég.
- **Forrásmegjelölés.** Az oldal láblécében szerepel az ENTSO-E Transparency Platform mint forrás. Nyilvános közzététel előtt érdemes elolvasni az ENTSO-E felhasználási feltételeit.
