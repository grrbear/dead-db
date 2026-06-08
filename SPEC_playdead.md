# SPEC_playdead.md — Play Dead availability tables (Phase A)

Status: ready to implement. Author: architecture conversation in Claude.ai.
Instruction to Claude Code: **implement verbatim; stop and ask before improvising.**

## What this is

[Play Dead](https://playdead.app) (launched April 2026, nugs + Rhino + Grateful
Dead Productions) is the official hi-res streaming app for the GD vault. This
phase adds **structured availability data** to `dead.db` — "which shows/releases
are streamable on Play Dead" — keyed on `show_date`, exactly like
`archive_recordings` and `plex_albums`. It is **not** lore; nothing touches
`dead_lore.db`.

The payoff (later) is that `dead_show_recordings(date)` can show an official
"streaming on Play Dead" line. **This spec builds the tables only. No MCP tool
changes, no share links, no auth.** Those are Phase B (`SPEC_playdead_links.md`,
not yet written).

## Data source (locked)

A single, regularly-updated nugs help-desk page (≈2 new shows added every
Tuesday, so counts only grow):

```
https://help.nugs.net/support/solutions/articles/6000284124-what-shows-albums-are-available-on-play-dead-
```

It contains two HTML `<table>`s:
- **Shows** — 4 columns: `Show Date | Venue Name | City | State/Country`.
  ~380 rows, dates `M/D/YY`, range 7/3/66 … 7/9/95. One row per show date.
  This is the chronological "Vault by Year" per-date spine.
- **Albums** — 1 column: official release titles (studio LPs, comps, Dave's
  Picks, Dick's Picks, Road Trips, box sets). ~150 rows. Many embed show
  date(s) in the title; studio/comp titles do not.

Note: the page also links to *"Why are some official live releases missing from
Play Dead?"* — so the Albums list is **deliberately not the full official
discography**. Gaps are expected, not a parse bug.

## Locked design decisions (do not relitigate)

- **Structured data → dead.db**, alongside `archive_recordings`/`plex_albums`.
  Three new tables. Joins to `shows.date` on the ISO `show_date` string.
- **Stdlib only** (`urllib`, `re`, `sqlite3`, `html.parser`). No bs4/lxml/requests
  — matches `scrape_archive.py` and `lore/fetchers/_html.py`. No new deps.
- **Single idempotent builder** `build_playdead.py` at repo root (next to
  `build_archive.py`). One HTML page → no cursor scrape. It caches the raw HTML
  to `playdead_raw.html` so re-parsing and week-to-week diffing don't re-fetch.
- **DROP + rebuild** all three tables every run (like `build_archive.py`). No
  change to existing tables.
- **`release_id` / `web_url` columns ship NULL.** They are the Play Dead
  `vault.playdead.app/release/<id>` deep-link fields, backfilled in Phase B.
  Schema is link-ready now so Phase B is a pure UPDATE, no migration.
- **Date parsing:** `M/D/YY` → `YYYY-MM-DD`, century always 19xx
  (`year = 1900 + YY`); valid range clamped to 1965–1995 (the band's active
  years). Anything outside is treated as unparseable and logged, not stored.
- **Album→date junction is best-effort.** Extract every explicit `M/D/YY` token
  from the title. Hyphen ranges (`2/3/78 - 2/5/78`) capture only the endpoints —
  acceptable, because `playdead_shows` is the authoritative per-date spine; the
  junction only *attributes which release covers a date*.
- **Parse-bug detector:** every `playdead_shows.show_date` that does **not**
  join to `shows.date` is written to `playdead_unresolved.log` (same spirit as
  `unresolved_titles.log`). Expected count ≈ 0.

## Schema (added to dead.db)

```sql
CREATE TABLE playdead_shows(
    show_date  TEXT PRIMARY KEY,   -- ISO YYYY-MM-DD; joins shows.date
    venue      TEXT,
    city       TEXT,
    state      TEXT,               -- "State/Country" col: CA, NY, England, Ontario, …
    release_id TEXT,               -- nugs release id; NULL until Phase B
    web_url    TEXT,               -- https://vault.playdead.app/release/<id>; NULL until Phase B
    fetched_at TEXT NOT NULL);
CREATE INDEX idx_playdead_shows_date ON playdead_shows(show_date);

CREATE TABLE playdead_albums(
    title      TEXT PRIMARY KEY,   -- exact title text from the page
    raw_dates  TEXT,               -- comma-joined ISO dates parsed from title; NULL if none
    release_id TEXT,               -- NULL until Phase B
    web_url    TEXT,               -- NULL until Phase B
    fetched_at TEXT NOT NULL);

CREATE TABLE playdead_album_shows(   -- album -> show_date (best-effort)
    title      TEXT NOT NULL,         -- = playdead_albums.title
    show_date  TEXT NOT NULL,         -- ISO date parsed from the title
    PRIMARY KEY(title, show_date));
CREATE INDEX idx_playdead_album_shows_date ON playdead_album_shows(show_date);
```

## File: build_playdead.py (implement verbatim)

```python
#!/usr/bin/env python3
"""Build Play Dead availability tables in dead.db from the nugs help-desk
catalog page (regularly updated; ~2 new shows every Tuesday).

  fetch help page  -> playdead_raw.html      (cached; gitignored)
  parse two tables -> playdead_shows         (date-keyed; joins shows.date)
                      playdead_albums         (official-release catalog)
                      playdead_album_shows    (album -> show_date junction)

Idempotent: DROP + rebuild all three tables. No change to existing tables.
release_id / web_url ship NULL -- backfilled in the Phase B links spec.
"""
import os, re, sys, sqlite3, urllib.request, datetime
from html.parser import HTMLParser

DB_PATH  = os.environ.get("DEAD_DB", "data/dead.db")
SRC_URL  = os.environ.get("PLAYDEAD_URL",
    "https://help.nugs.net/support/solutions/articles/"
    "6000284124-what-shows-albums-are-available-on-play-dead-")
RAW_PATH = os.environ.get("PLAYDEAD_RAW", "playdead_raw.html")
NO_FETCH = os.environ.get("PLAYDEAD_NO_FETCH", "") not in ("", "0")
UA = {"User-Agent": "deadbase-playdead/0.1 (homelab personal use)"}

SHOWS_FLOOR  = 350   # ~380 at time of writing; monotonic. Sanity gate only.
ALBUMS_FLOOR = 120   # ~150 at time of writing.

# ---- fetch -----------------------------------------------------------------
def fetch(url, out_path):
    req = urllib.request.Request(url, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"fetched {len(html)} bytes -> {out_path}", file=sys.stderr)
    return html

# ---- table parsing (stdlib html.parser) ------------------------------------
class _TableParser(HTMLParser):
    """Collect every <table> as a list of rows; each row a list of cell texts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._t, self._row, self._cell = [], None, None, None
    def handle_starttag(self, tag, attrs):
        if tag == "table": self._t = []
        elif tag == "tr" and self._t is not None: self._row = []
        elif tag in ("td", "th") and self._row is not None: self._cell = []
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(_collapse("".join(self._cell))); self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row): self._t.append(self._row)
            self._row = None
        elif tag == "table" and self._t is not None:
            self.tables.append(self._t); self._t = None
    def handle_data(self, data):
        if self._cell is not None: self._cell.append(data)

def _collapse(s):
    return re.sub(r"\s+", " ", s).strip()

# ---- date helpers ----------------------------------------------------------
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")

def mdy_to_iso(m, d, yy):
    mo, da, yr = int(m), int(d), 1900 + int(yy)   # all GD shows are 19xx
    if not (1 <= mo <= 12 and 1 <= da <= 31): return None
    if not (1965 <= yr <= 1995): return None
    return f"{yr:04d}-{mo:02d}-{da:02d}"

def iso_dates_in(text):
    out = []
    for m in _DATE_RE.finditer(text):
        iso = mdy_to_iso(*m.groups())
        if iso and iso not in out: out.append(iso)
    return out

# ---- classify --------------------------------------------------------------
def classify(tables):
    """Pick the Shows table (most 4-col rows whose 1st cell is a date) and the
    Albums table (the largest 1-col table). Robust to other page tables/order."""
    best_shows, best_albums = [], []
    for t in tables:
        date_rows = [r for r in t if len(r) >= 4 and _DATE_RE.match(r[0] or "")]
        if len(date_rows) > len(best_shows): best_shows = date_rows
        one_col = [r[0] for r in t if len(r) == 1 and r[0]]
        if len(one_col) > len(best_albums): best_albums = one_col
    if len(best_shows) < SHOWS_FLOOR:
        sys.exit(f"Shows table not found / too small ({len(best_shows)})")
    if len(best_albums) < ALBUMS_FLOOR:
        sys.exit(f"Albums table not found / too small ({len(best_albums)})")
    return best_shows, best_albums

# ---- build -----------------------------------------------------------------
def build(con, shows_rows, album_titles):
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    cur = con.cursor()
    for ddl in ("DROP TABLE IF EXISTS playdead_shows",
                "DROP TABLE IF EXISTS playdead_albums",
                "DROP TABLE IF EXISTS playdead_album_shows"):
        cur.execute(ddl)
    cur.execute("""CREATE TABLE playdead_shows(
        show_date TEXT PRIMARY KEY, venue TEXT, city TEXT, state TEXT,
        release_id TEXT, web_url TEXT, fetched_at TEXT NOT NULL)""")
    cur.execute("CREATE INDEX idx_playdead_shows_date ON playdead_shows(show_date)")
    cur.execute("""CREATE TABLE playdead_albums(
        title TEXT PRIMARY KEY, raw_dates TEXT,
        release_id TEXT, web_url TEXT, fetched_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE playdead_album_shows(
        title TEXT NOT NULL, show_date TEXT NOT NULL,
        PRIMARY KEY(title, show_date))""")
    cur.execute("CREATE INDEX idx_playdead_album_shows_date ON playdead_album_shows(show_date)")

    unresolved, n_shows = [], 0
    for r in shows_rows:
        m = _DATE_RE.match(r[0] or "")
        iso = mdy_to_iso(*m.groups()) if m else None
        if not iso:
            unresolved.append(r[0]); continue
        cur.execute("INSERT OR REPLACE INTO playdead_shows VALUES(?,?,?,?,?,?,?)",
            (iso, (r[1] or "").strip()[:200], (r[2] or "").strip()[:120],
             (r[3] or "").strip()[:80], None, None, now))
        n_shows += 1

    n_albums = n_junction = 0
    for title in album_titles:
        title = title.strip()[:300]
        dates = iso_dates_in(title)
        cur.execute("INSERT OR REPLACE INTO playdead_albums VALUES(?,?,?,?,?)",
            (title, (",".join(dates) or None), None, None, now))
        n_albums += 1
        for d in dates:
            cur.execute("INSERT OR REPLACE INTO playdead_album_shows VALUES(?,?)",
                (title, d)); n_junction += 1
    con.commit()

    # parse-bug detector: playdead dates that don't match a known show
    nomatch = [row[0] for row in cur.execute(
        """SELECT ps.show_date FROM playdead_shows ps
           LEFT JOIN shows s ON s.date = ps.show_date
           WHERE s.date IS NULL ORDER BY ps.show_date""").fetchall()]
    with open("playdead_unresolved.log", "w") as f:
        for d in unresolved: f.write(f"UNPARSED\t{d}\n")
        for d in nomatch:    f.write(f"NO_SHOW_MATCH\t{d}\n")

    assert n_shows  >= SHOWS_FLOOR,  f"expected >={SHOWS_FLOOR} shows, got {n_shows}"
    assert n_albums >= ALBUMS_FLOOR, f"expected >={ALBUMS_FLOOR} albums, got {n_albums}"
    print(f"playdead_shows:  {n_shows}  ({len(nomatch)} unmatched to shows, "
          f"{len(unresolved)} unparsed)")
    print(f"playdead_albums: {n_albums}   album_shows junction: {n_junction}")
    print("wrote playdead_unresolved.log  [validation OK]")

if __name__ == "__main__":
    if NO_FETCH:
        if not os.path.exists(RAW_PATH): sys.exit(f"missing {RAW_PATH} and PLAYDEAD_NO_FETCH set")
        html = open(RAW_PATH, encoding="utf-8").read()
    else:
        html = fetch(SRC_URL, RAW_PATH)
    p = _TableParser(); p.feed(html); p.close()
    shows_rows, album_titles = classify(p.tables)
    con = sqlite3.connect(DB_PATH); build(con, shows_rows, album_titles); con.close()
```

## Run

```bash
cd /home/bear/dead-db
DEAD_DB=/hddpool/datastore/dead.db python3 build_playdead.py
# re-parse cached HTML without re-fetching:
DEAD_DB=/hddpool/datastore/dead.db PLAYDEAD_NO_FETCH=1 python3 build_playdead.py
```

Also add to `.gitignore`: `playdead_raw.html`

## Success criteria (verify after build)

1. Build prints `[validation OK]`; `playdead_shows` ≈ 380, `playdead_albums` ≈ 150.
2. `playdead_unresolved.log` has **zero `NO_SHOW_MATCH` lines** (or a tiny number
   you can eyeball as legit date-spelling differences vs. gdshowsdb). If it has
   many, the parse or the date logic broke — STOP.
3. Spot checks:
   ```sql
   SELECT * FROM playdead_shows WHERE show_date='1977-05-08';   -- Barton Hall, Cornell, Ithaca NY
   SELECT MIN(show_date), MAX(show_date), COUNT(*) FROM playdead_shows;  -- 1966-07-03 .. 1995-07-09
   SELECT * FROM playdead_albums WHERE title='American Beauty';  -- raw_dates NULL
   SELECT * FROM playdead_album_shows
     WHERE title LIKE 'Dave''s Picks, Volume 1:%';               -- -> 1977-05-25
   -- coverage vs the rest of the DB
   SELECT COUNT(*) FROM playdead_shows ps JOIN plex_albums pa ON pa.show_date=ps.show_date; -- owned ∩ playdead
   ```
4. Existing tables untouched; existing `dead_*` MCP tools behave identically
   (no tool code changed in this phase).

## Out of scope (this spec)

- No `release_id` / `web_url` values (columns stay NULL).
- No nugs auth, no catalog API, no `vault.playdead.app` crawl, no sitemap.
- No MCP tool changes (`dead_show_recordings`, `dead_stats` untouched).
- No streaming/playback (separate Music Assistant investigation).
- No `dead_lore.db` / RAG changes.
- No album hyphen-range expansion beyond endpoint dates.
- **No git commit.** Leave the working tree for John to diff + commit.

## Phase B (next spec — DO NOT build here)

`SPEC_playdead_links.md` will populate `release_id` / `web_url` and then wire the
MCP tools. Findings already established for it:

- The shareable, web-openable link is **`https://vault.playdead.app/release/<release_id>`**.
  `live.gd/<code>` and `share.playdead.app/release/<id>` both redirect there;
  `playdead://release/<id>` is the app deep link. The id is an **opaque nugs
  release id, not derivable from a date** (e.g. 4/5/89 = 48401, 9/22/88 = 48403).
- `vault.playdead.app/release/<id>` pages render full OG metadata
  (`og:title` = "Grateful Dead - M-D-YYYY Venue City, ST", `og:image` cover whose
  filename encodes the date as `gd<YYMMDD>_NN.jpg`) **unauthenticated**. So the
  catalog is probably enumerable with **no auth**.
- Phase B probe order: (1) try `vault.playdead.app/sitemap.xml` / a public listing
  to harvest all `release/<id>` + dates, no auth; (2) only if that fails, fall
  back to the authenticated nugs catalog API (credentials via **env vars only**,
  reusing the `/tmp/playdead_probe` pattern). Backfill `playdead_shows.release_id`
  by date join, `playdead_albums.release_id` by title match. Then wire tools.
```

This pattern matches build_archive.py.
