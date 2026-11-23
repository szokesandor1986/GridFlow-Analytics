#!/usr/bin/env python3
"""A dist/ mappa fájljait feltölti egy webtárhelyre FTPS-en (FTP TLS-sel).

Környezeti változók:
    FTP_HOST   a tárhely FTP-címe (kötelező)
    FTP_USER   felhasználónév (kötelező)
    FTP_PASS   jelszó (kötelező)
    FTP_DIR    a távoli célmappa, pl. /public_html vagy /www (alapértelmezés: a belépési mappa)
    FTP_PORT   port (alapértelmezés: 21)
    FTP_CAFILE csak tesztelésre: saját CA-tanúsítvány (éles használatnál ne állítsd be)

A kapcsolat titkosítása és a tanúsítvány ellenőrzése kötelező. Ha a tárhely csak sima FTP-t tud,
a szkript hibával leáll, és a jelszó nem megy ki titkosítatlanul.
Az adatfájlt először ideiglenes néven tölti fel, majd átnevezi, így a látogató nem kap fél fájlt.
"""
import ftplib
import os
import pathlib
import ssl
import sys


def put(ftp, local, name):
    tmp = name + ".tmp"
    with open(local, "rb") as f:
        ftp.storbinary("STOR " + tmp, f)
    try:
        ftp.rename(tmp, name)
    except ftplib.error_perm:
        # néhány szerver nem írja felül a meglévő fájlt átnevezéssel
        ftp.delete(name)
        ftp.rename(tmp, name)


def main():
    try:
        host, user, pw = os.environ["FTP_HOST"], os.environ["FTP_USER"], os.environ["FTP_PASS"]
    except KeyError as e:
        sys.exit(f"Hiányzó környezeti változó: {e.args[0]}")
    port = int(os.environ.get("FTP_PORT", "21"))
    remote_dir = os.environ.get("FTP_DIR", "").strip()
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    files = sorted((p for p in src.iterdir() if p.is_file()), key=lambda p: (p.name == "data.json", p.name))
    if not any(p.name == "data.json" for p in files):
        sys.exit(f"Nincs {src}/data.json, nincs mit feltölteni.")

    ctx = ssl.create_default_context(cafile=os.environ.get("FTP_CAFILE") or None)
    with ftplib.FTP_TLS(context=ctx, timeout=30) as ftp:
        ftp.connect(host, port)
        ftp.login(user, pw)  # a login() előbb AUTH TLS-t kér
        ftp.prot_p()  # az adatcsatorna is titkosított
        if remote_dir:
            ftp.cwd(remote_dir)
        for p in files:
            put(ftp, p, p.name)
            print("feltöltve:", p.name, p.stat().st_size, "bájt")


if __name__ == "__main__":
    try:
        main()
    except (*ftplib.all_errors, ssl.SSLError) as e:
        sys.exit(f"Feltöltési hiba: {type(e).__name__}: {e}")
