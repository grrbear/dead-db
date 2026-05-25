# dead-db

A normalized SQLite database of Grateful Dead setlists, joined to your Plex music library. Structured facts answered by SQL — not by an LLM guessing.

## What it does

- Loads all 2,358 Grateful Dead shows and 39,774 song performances from [gdshowsdb](https://github.com/jefmsmit/gdshowsdb) YAML into a relational SQLite DB
- Pulls all albums from your Plex "Grateful Dead" library section, extracts show dates from album titles, and writes a `plex_albums` join table
- Exposes 5 MCP tools via [homelab-mcp](https://github.com/grrbear/homelab) so you can ask Claude questions like "what did they play at Cornell 77" or "every time they played Scarlet > Fire"

## Schema

```sql
songs        (uuid PK, name)
shows        (date PK, uuid, venue, city, state, country)
performances (id PK, show_date FK, set_num, position, song_uuid FK, song_name, segued_out)
plex_albums  (rating_key PK, title, year, show_date)   -- added by plex.py
```

`plex_albums.show_date` joins to `shows.date` — this is how "what's in your library" queries work.

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

# 4. Build the shows/performances tables (writes to /hddpool/datastore/dead.db by default)
python3 build_db.py

# 5. Add your Plex library as plex_albums
PLEX_TOKEN=<your-token> python3 plex.py
```

Both scripts respect env vars:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/hddpool/datastore/dead.db` | SQLite output path |
| `PLEX_URL` | `http://192.168.0.5:32400` | Plex server URL |
| `PLEX_TOKEN` | *(required)* | Plex auth token |
| `GD_SECTION` | *(auto-detected)* | Plex library section ID — found by title match on "dead"/"grateful" |

## Rebuild

`gddata/` and `data/` are gitignored. To rebuild from scratch:

```bash
python3 build_db.py         # drops and recreates songs/shows/performances
PLEX_TOKEN=xxx python3 plex.py   # drops and recreates plex_albums
```

`build_db.py` validates its output and will fail loudly if upstream YAML data changes shape:
- ≥ 2358 shows
- ≥ 39774 performances
- ≥ 526 songs

## MCP Tools

Five tools exposed via homelab-mcp (`https://mcp.quickswoodcapital.com/mcp`):

| Tool | Description |
|---|---|
| `dead_stats` | Totals, top 10 songs, top 10 venues, most active years |
| `dead_setlist(date)` | Full set-by-set setlist for any show, with segue markers and Plex flag |
| `dead_song_history(song, year, limit)` | Every performance of a song; filterable by year |
| `dead_shows(year, venue, city, song, limit)` | Filter shows — find all dates where a song appeared |
| `dead_plex_library(query, limit)` | Your Plex GD albums with venue info and ratingKeys (for playlist tools) |

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
```

## File layout

```
dead-db/
  build_db.py          # loads gdshowsdb YAML → songs/shows/performances
  plex.py              # pulls Plex GD library → plex_albums
  requirements.txt     # pyyaml, requests
  unresolved_titles.log  # the 119 Plex albums without a dateable title (expected)
  gddata/              # gitignored — clone of jefmsmit/gdshowsdb
  data/                # gitignored — local DB_PATH override output
```

## Data source

Setlist data from [jefmsmit/gdshowsdb](https://github.com/jefmsmit/gdshowsdb) (MIT license).
