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


def _extract_id(uri):
    """Strip plex://type/ prefix, returning bare hex ID. None for local:// items."""
    if uri and uri.startswith("plex://"):
        parts = uri.split("/")
        return parts[-1] if len(parts) >= 3 else None
    return None


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


def fetch_guid(rk):
    """Fetch the global guid + parentGuid for one album (only on the item endpoint).

    Returns bare IDs (e.g. '5d07cc7f403c640290e8646b') stripped of the plex://type/
    prefix, ready for Plexamp link construction. Returns (None, None) for unmatched
    items whose guid is a local:// URI rather than a plex:// one.
    """
    root = _plex(f"/library/metadata/{rk}")
    el = root.find(".//Directory")
    if el is None:
        return None, None
    return _extract_id(el.get("guid")), _extract_id(el.get("parentGuid"))


def fetch_tracks(album_rk, show_date):
    """Fetch all tracks for an album via /children. guid is present on Track (Path A)."""
    root = _plex(f"/library/metadata/{album_rk}/children")
    rows = []
    for t in root.findall(".//Track"):
        trk = int(t.get("ratingKey"))
        guid = _extract_id(t.get("guid"))
        rows.append((
            trk,
            album_rk,
            show_date,
            int(t.get("parentIndex") or 1),
            int(t.get("index") or 0),
            t.get("title", ""),
            guid,
        ))
    return rows


def sync(db_path=DB_PATH):
    if not PLEX_TOKEN:
        sys.exit("PLEX_TOKEN not set")

    section_id = find_gd_section()
    albums = fetch_albums(section_id)

    # Enrich with guid/parentGuid (needed for Plexamp deep links; not in the /all lister)
    enriched = []
    for i, (rk, title, yr, sd) in enumerate(albums, 1):
        guid, pguid = fetch_guid(rk)
        enriched.append((rk, title, yr, sd, guid, pguid))
        if i % 50 == 0:
            print(f"  fetched guids {i}/{len(albums)}...")
    albums = enriched

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS plex_albums;
        CREATE TABLE plex_albums (
            rating_key  INTEGER PRIMARY KEY,
            title       TEXT NOT NULL,
            year        INTEGER,
            show_date   TEXT,
            guid        TEXT,
            parent_guid TEXT
        );
        CREATE INDEX idx_pa_date ON plex_albums(show_date);
        DROP TABLE IF EXISTS plex_tracks;
        CREATE TABLE plex_tracks (
            track_rating_key INTEGER PRIMARY KEY,
            album_rating_key INTEGER,
            show_date        TEXT,
            disc             INTEGER,
            position         INTEGER,
            title            TEXT,
            track_guid       TEXT
        );
        CREATE INDEX idx_pt_album ON plex_tracks(album_rating_key);
        CREATE INDEX idx_pt_date  ON plex_tracks(show_date);
    """)
    cur.executemany("INSERT INTO plex_albums VALUES(?,?,?,?,?,?)", albums)
    con.commit()

    total    = len(albums)
    resolved = sum(1 for a in albums if a[3])
    nulls    = [a for a in albums if not a[3]]
    with_guid = sum(1 for a in albums if a[4])
    print(f"plex_albums: {total} albums, {resolved} dated, {len(nulls)} NULL, {with_guid} with guid")

    if nulls:
        print("\nNULL (no YYYY-MM-DD in title):")
        for rk, title, yr, _, _g, _pg in nulls:
            print(f"  [{rk}] {title}")

    # detect duplicate show_dates (multiple ratingKeys for same date)
    seen = {}
    for a in albums:
        if a[3]:
            seen.setdefault(a[3], []).append(a)
    dups = {d: v for d, v in seen.items() if len(v) > 1}

    if dups:
        print("\nDuplicate dates (multiple albums for same show):")
        for date, entries in sorted(dups.items()):
            print(f"  {date}:")
            for rk, title, yr, _, _g, _pg in entries:
                print(f"    [{rk}] {title}")

    # Backfill plex_tracks for owned ∩ voted shows
    try:
        voted = {r[0] for r in cur.execute(
            "SELECT DISTINCT show_date FROM community_votes WHERE show_date IS NOT NULL")}
    except sqlite3.OperationalError:
        voted = set()
        print("community_votes not found — skipping plex_tracks backfill")

    qualifying = [(rk, sd) for rk, title, yr, sd, guid, pguid in albums
                  if sd and sd in voted]
    print(f"\nBackfilling plex_tracks for {len(qualifying)} owned+voted albums...")
    track_rows = []
    for i, (rk, sd) in enumerate(qualifying, 1):
        track_rows.extend(fetch_tracks(rk, sd))
        if i % 25 == 0:
            print(f"  fetched tracks {i}/{len(qualifying)}...")
    cur.executemany("INSERT OR REPLACE INTO plex_tracks VALUES(?,?,?,?,?,?,?)", track_rows)
    con.commit()
    album_count = len({r[1] for r in track_rows})
    print(f"plex_tracks: {len(track_rows)} tracks across {album_count} owned+voted albums")

    con.close()
    print(f"\nWrote plex_albums + plex_tracks → {db_path}")


if __name__ == "__main__":
    sync()
