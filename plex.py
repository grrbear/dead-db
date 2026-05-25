"""
plex.py — Pull Grateful Dead albums from Plex and sync to dead.db.

Queries the Plex 'Grateful Dead' library, extracts show dates from album
titles using ^(\d{4}-\d{2}-\d{2}), and writes a plex_albums table into
dead.db for joining against shows/performances.

IMPORTANT: show dates come from the title regex, NEVER from the Plex `year`
field. Plex stores release year (e.g. 1993 for Dick's Picks Vol. 1), not
the show year (1973). Using `year` silently mis-dates every archival release.

env:
  PLEX_URL    default http://192.168.0.5:32400
  PLEX_TOKEN  required
  DB_PATH     default data/dead.db
  GD_SECTION  Plex section ID — auto-detected from title if unset
"""
import os, re, sqlite3, sys
from xml.etree import ElementTree as ET
import requests

DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})')

PLEX_URL   = os.environ.get("PLEX_URL",   "http://192.168.0.5:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DB_PATH    = os.environ.get("DB_PATH",    "/hddpool/datastore/dead.db")
GD_SECTION = os.environ.get("GD_SECTION", "")


def _plex(path, **params):
    params["X-Plex-Token"] = PLEX_TOKEN
    r = requests.get(f"{PLEX_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


def find_gd_section():
    if GD_SECTION:
        return GD_SECTION
    root = _plex("/library/sections")
    for d in root.findall(".//Directory"):
        title = d.get("title", "")
        if "dead" in title.lower() or "grateful" in title.lower():
            print(f"GD section: [{d.get('key')}] {title}")
            return d.get("key")
    raise RuntimeError("Grateful Dead library not found — set GD_SECTION env var")


def fetch_albums(section_id):
    root = _plex(f"/library/sections/{section_id}/all", type=9)
    albums = []
    for d in root.findall(".//Directory"):
        rk    = int(d.get("ratingKey"))
        title = d.get("title", "")
        yr    = d.get("year")
        m     = DATE_RE.match(title)
        albums.append((rk, title, int(yr) if yr else None, m.group(1) if m else None))
    return albums


def sync(db_path=DB_PATH):
    if not PLEX_TOKEN:
        sys.exit("PLEX_TOKEN not set")

    section_id = find_gd_section()
    albums = fetch_albums(section_id)

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS plex_albums;
        CREATE TABLE plex_albums (
            rating_key INTEGER PRIMARY KEY,
            title      TEXT NOT NULL,
            year       INTEGER,
            show_date  TEXT
        );
        CREATE INDEX idx_pa_date ON plex_albums(show_date);
    """)
    cur.executemany("INSERT INTO plex_albums VALUES(?,?,?,?)", albums)
    con.commit()

    total    = len(albums)
    resolved = sum(1 for a in albums if a[3])
    nulls    = [a for a in albums if not a[3]]

    # detect duplicate show_dates (multiple ratingKeys for same date)
    seen = {}
    for a in albums:
        if a[3]:
            seen.setdefault(a[3], []).append(a)
    dups = {d: v for d, v in seen.items() if len(v) > 1}

    print(f"plex_albums: {total} albums, {resolved} dated, {len(nulls)} NULL")

    if nulls:
        print("\nNULL (no YYYY-MM-DD in title):")
        for rk, title, yr, _ in nulls:
            print(f"  [{rk}] {title}")

    if dups:
        print("\nDuplicate dates (multiple albums for same show):")
        for date, entries in sorted(dups.items()):
            print(f"  {date}:")
            for rk, title, yr, _ in entries:
                print(f"    [{rk}] {title}")

    con.close()
    print(f"\nWrote plex_albums → {db_path}")


if __name__ == "__main__":
    sync()
