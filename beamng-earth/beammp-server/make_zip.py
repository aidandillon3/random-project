"""Packs Resources/Client/beamng_earth_mp/ into Resources/Client/beamng_earth_mp.zip for the BeamMP server.

Usage: python make_zip.py

Every entry gets a fixed date in the past (2026-10-21 08:00; 1.5.3 used 2026-10-19 12:00): a zip dated in the players' future made BeamNG.Earth
rebuild its terrain cache over and over (freezes). Swapping the zip on a running server needs a server restart
(or `reloadmods`), and everyone rejoins.
"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "Resources", "Client", "beamng_earth_mp")
OUT = os.path.join(HERE, "Resources", "Client", "beamng_earth_mp.zip")
DT = (2026, 10, 21, 8, 0, 0)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(SRC):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            zi = zipfile.ZipInfo(os.path.relpath(p, SRC).replace(os.sep, "/"), DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with open(p, "rb") as fh:
                z.writestr(zi, fh.read())
print("wrote", OUT, os.path.getsize(OUT), "bytes")
