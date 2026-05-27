# dead-db

A normalized SQLite database of Grateful Dead setlists, joined to your Plex music library and archive.org recordings. Structured facts answered by SQL — not by an LLM guessing.

## What it does

- Loads all 2,358 Grateful Dead shows and 39,774 song performances from [gdshowsdb](https://github.com/jefmsmit/gdshowsdb) YAML into a relational SQLite DB
- Pulls all albums from your Plex "Grateful Dead" library section, extracts show dates from album titles, and writes a `plex_albums` join table
- Scrapes 18,224 recordings from the [archive.org GratefulDead collection](https://archive.org/details/GratefulDead) and writes an `archive_recordings` table with source classification and rankings
- Exposes 11 MCP tools via [homelab-mcp](https://github.com/grrbear/homelab) (`homelab-mcp/tools/deaddb.py`) so you can ask Claude questions like "what did they play at Cornell 77", "every time they played Scarlet > Fire", or "how can I hear the Veneta 72 show"

## Schema

```sql
songs              (uuid PK, name)
shows              (date PK, uuid, venue, city, state, country)
performances       (id PK, show_date FK, set_num, position, song_uuid FK, song_name, segued_out)
plex_albums        (rating_key PK, title, year, show_date)          -- added by plex.py
archive_recordings (identifier PK, show_date, source_type, venue,   -- added by build_archive.py
                    coverage, avg_rating, num_reviews, downloads, archive_url)
```

`plex_albums.show_date` and `archive_recordings.show_date` both join to `shows.date`.

### Critical: show date extraction

Show dates come from the **album title** via regex `^(\d{4}-\d{2}-\d{2})`, never from Plex's `year` field.

Plex stores the **release year** of the archival product (e.g. 1993 for Dick's Picks Vol. 1), not the show year (1973). Using the `year` field silently mis-dates every Dick's Picks, Dave's Picks, and Road Trips release in the library.

Albums without a leading `YYYY-MM-DD` in the title (studio albums, compilations, month-only boots, editorial mixes) resolve to `NULL` and are expected. Current library: 595 total, 476 dated, 119 NULL — all NULLs are accounted for in `unresolved_titles.log`.

## Setup

```bash
# 1. Clone this repo
git clone https://github.com/grrbear/dead-db.git && cd dead-db

# 2. Install deps
pip install -r requirements.txt

# 3. Clone the setlist source data
git clone https://github.com/jefmsmit/gdshowsdb.git gddata

# 4. Build the shows/performances tables
python3 build_db.py

# 5. Add your Plex library as plex_albums
PLEX_TOKEN=<your-token> python3 plex.py

# 6. Scrape archive.org (~10 min, ~18k recordings)
python3 scrape_archive.py

# 7. Build archive_recordings table
python3 build_archive.py
```

`build_db.py` and `plex.py` respect `DB_PATH` (default `/hddpool/datastore/dead.db`).
`build_archive.py` reads `DEAD_DB` (default `data/dead.db`) and `ARCHIVE_RAW` (default `archive_raw.jsonl`).

| Variable | Script | Default | Description |
|---|---|---|---|
| `DB_PATH` | build_db.py, plex.py | `/hddpool/datastore/dead.db` | SQLite output path |
| `PLEX_URL` | plex.py | `http://192.168.0.5:32400` | Plex server URL |
| `PLEX_TOKEN` | plex.py | *(required)* | Plex auth token |
| `GD_SECTION` | plex.py | *(auto-detected)* | Plex library section ID |
| `DEAD_DB` | build_archive.py | `data/dead.db` | SQLite output path |
| `ARCHIVE_RAW` | build_archive.py | `archive_raw.jsonl` | Scrape output from scrape_archive.py |

## Rebuild

`gddata/`, `data/`, and `archive_raw.jsonl` are gitignored. Full rebuild from scratch:

```bash
python3 build_db.py                                        # songs/shows/performances
PLEX_TOKEN=xxx python3 plex.py                             # plex_albums
python3 scrape_archive.py                                  # archive_raw.jsonl (~10 min)
DEAD_DB=/hddpool/datastore/dead.db python3 build_archive.py  # archive_recordings
```

Validation floors (all scripts fail loudly if breached):
- `build_db.py`: ≥ 2358 shows, ≥ 39774 performances, ≥ 526 songs
- `build_archive.py`: ≥ 18000 recordings, ≥ 2000 distinct show dates

### Archive source classification

`build_archive.py` classifies each recording's `source_type` from the identifier and source string:

| Type | Meaning |
|---|---|
| `SBD` | Soundboard recording |
| `MATRIX` | Soundboard + audience mix |
| `FM` | FM broadcast |
| `AUD` | Audience recording |
| `UNKNOWN` | Unclassifiable |

## MCP Tools

Eleven tools exposed via homelab-mcp (`https://mcp.quickswoodcapital.com/mcp`):

| Tool | Description |
|---|---|
| `dead_stats` | Totals, top 10 songs/venues, most active years (top 10), archive.org coverage |
| `dead_setlist(date)` | Full set-by-set setlist for any show, with segue markers and Plex flag |
| `dead_song_history(song, year, limit)` | Every performance of a song; filterable by year |
| `dead_shows(year, venue, city, song, limit)` | Filter shows — find all dates where a song appeared |
| `dead_plex_library(query, limit)` | Your Plex GD albums with venue info and ratingKeys (for playlist tools) |
| `dead_show_recordings(date)` | Owned Plex copies + top-ranked archive.org recordings for any show |
| `dead_this_date(month, day)` | Every show the Dead played on this calendar date across all years |
| `dead_song_stats(song)` | Deep stats: first/last played, longest gap, set distribution, decade breakdown |
| `dead_segues(song_a, song_b)` | All `A > B` occurrences, or top 10 songs `A` segued into |
| `dead_run(date)` | Full tour run context for any show — all adjacent dates (≤3-day gap) |
| `dead_rare_songs(year, max_plays)` | Songs played ≤N times overall or within a single year |

`dead_show_recordings` ranking: recordings with ≥3 reviews sorted by rating, tie-broken by source quality (SBD/MATRIX > FM > AUD) then downloads.

## Example queries

```sql
-- Cornell 77 setlist
SELECT set_num, position, song_name, segued_out
FROM performances WHERE show_date = '1977-05-08'
ORDER BY set_num, position;

-- Every Scarlet Begonias > Fire on the Mountain pairing
SELECT p1.show_date, s.venue, s.city
FROM performances p1
JOIN performances p2 ON p2.show_date = p1.show_date
  AND p2.set_num = p1.set_num AND p2.position = p1.position + 1
  AND p2.song_name LIKE '%Fire on the Mountain%'
JOIN shows s ON s.date = p1.show_date
WHERE p1.song_name LIKE '%Scarlet%' AND p1.segued_out = 1
ORDER BY p1.show_date;

-- Shows in Plex library from 1972
SELECT pa.show_date, pa.title, s.venue
FROM plex_albums pa JOIN shows s ON s.date = pa.show_date
WHERE pa.show_date LIKE '1972-%'
ORDER BY pa.show_date;

-- Best-rated archive recordings for a show
SELECT source_type, avg_rating, num_reviews, archive_url
FROM archive_recordings WHERE show_date = '1977-05-08'
ORDER BY CASE WHEN num_reviews>=3 THEN 0 ELSE 1 END, -COALESCE(avg_rating,0)
LIMIT 5;
```

## File layout

```
dead-db/
  build_db.py          # loads gdshowsdb YAML → songs/shows/performances
  plex.py              # pulls Plex GD library → plex_albums
  scrape_archive.py    # cursor scrape of archive.org GratefulDead → archive_raw.jsonl
  build_archive.py     # archive_raw.jsonl → archive_recordings table in dead.db
  requirements.txt     # pyyaml, requests
  unresolved_titles.log  # the 119 Plex albums without a dateable title (expected)
  gddata/              # gitignored — clone of jefmsmit/gdshowsdb
  data/                # gitignored — local DEAD_DB override output
  archive_raw.jsonl    # gitignored — scrape output (~18k lines)
```

## Data sources

- Setlist data: [jefmsmit/gdshowsdb](https://github.com/jefmsmit/gdshowsdb) (MIT license)
- Archive recordings: [archive.org GratefulDead collection](https://archive.org/details/GratefulDead) (public domain / CC)
