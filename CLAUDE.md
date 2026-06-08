# dead-db — project status

This file is the durable state of the dead-db project. Read it first when
opening this repo in a new session. It captures what's done, what's next,
and which design decisions are locked (so they're not relitigated).

## What this project is

A normalized SQLite database of Grateful Dead setlists, joined to my Plex
music library and archive.org recordings, with structured questions
answered by SQL and lore/insight questions answered by RAG. Exposed as
16 MCP tools via a dedicated **dead-mcp** server (https://dead-mcp.quickswoodcapital.com/mcp).

See README.md for end-user-facing description. This file is for picking
up development between sessions.

## Phase status

- [x] **Phase 1** — date-keyed `shows` + `plex_albums` joined on date
- [x] **Phase 2** — setlist/stats query engine + 11 MCP tools
- [x] **Phase 4** — archive.org gap-fill (built ahead of phase 3 because
      archive.org has a clean API; no regrets)
- [x] **Phase 3** — RAG over Grateful Dead lore
  - [x] Scaffolding (`lore/` package, smoke_test 16/16 passing)
  - [x] Wikipedia fetcher (`lore/fetchers/wikipedia.py`, ~110 curated articles)
  - [x] Light Into Ashes fetcher (`lore/fetchers/lia.py`, ~200 essays + primary sources)
  - [x] Books fetcher (`lore/fetchers/books.py`, EPUB library)
  - [x] Song-name matching (`lore/song_matcher.py`, `lore/match_songs.py`)
  - [x] MCP tools: `dead_lore` + `dead_ask`
  - [x] Router (`lore/router.py`) — entity extraction + hybrid retrieval + followup suggestions
  - [x] HeadyVersion ingest (`build_headyversion.py`, `lore/fetchers/headyversion.py`)
        25,012 community votes in `dead.db.community_votes`; 93% resolved to song_uuid;
        segue entries (China>Rider etc.) mapped to first-song UUID.
        MCP tools: `dead_top_versions` + `dead_show_votes`.
  - [x] Deadcast fetcher (`lore/fetchers/deadcast.py`, `lore/build_deadcast.py`)
        Local-HTML path — saved dead.net transcript pages on NAS. No network, no Whisper.
        88 episodes ingested, 4866 chunks. Idempotent; grows by
        dropping more saved pages into DEADCAST_DIR and re-running build_deadcast.
  - [x] Reddit fetcher (`lore/fetchers/reddit.py`, `lore/build_reddit.py`)
        Offline Arctic Shift dumps (.jsonl NDJSON) — no network, no auth.
        r/gratefuldead posts + comments dump in `lore/data/reddit/`. Two-pass:
        scan posts (score≥10 gate), attach qualifying comments (score≥5, len≥120),
        emit one doc per post. Slash-date annotation ("5/8/77" → ISO inline) so
        mentioned_dates populates for show-specific queries.
        Router Option B: PER_SOURCE_CAP=4 + SOURCE_WEIGHT=0.85 boost.
        Original live-API fetcher parked as `fetchers/reddit_api.py` (Reddit
        unauthenticated .json now 403s — Cloudflare bot block + RBP gate).
- [x] **Phase 5** — dead-mcp extraction: all 16 tools moved to dedicated server
      `dead_mcp/` in this repo, port 8768, https://dead-mcp.quickswoodcapital.com/mcp
      homelab-mcp rebuilt without torch/ML deps (2 GB → 315 MB)
- [x] **Phase A** — Play Dead availability tables (`build_playdead.py`)
      Scrapes the nugs help-desk catalog page and populates three tables in
      `dead.db`: `playdead_shows` (442 date-keyed rows, 1966-07-03..1995-07-09),
      `playdead_albums` (164 official releases), `playdead_album_shows` junction
      (110 rows). Idempotent DROP+rebuild. `release_id`/`web_url` NULL until Phase B.
      5 NO_SHOW_MATCH in log — all legit two-show days (gdshowsdb uses YYYY-MM-DD-N
      suffixes for same-day double-headers; Play Dead lists the date once).

## Phase 3 locked design decisions (do not relitigate)

These came out of an architecture conversation. Each looks arbitrary
without that context; that's expected. If something seems wrong, STOP
and ask before changing it.

- **Vector store:** sqlite-vec (not Chroma, not Qdrant, not FAISS).
- **Embedding model:** BAAI/bge-small-en-v1.5 at 384 dim, CPU inference
  on arrstack. The iGPU stays with Immich (and is reserved for future
  Whisper work on Deadcast).
- **Database file:** /hddpool/datastore/dead_lore.db, separate from
  dead.db. Joined by show_date string.
- **raw_text** is stored in the documents table — trades ~50-200 MB of
  disk for the ability to re-chunk without re-scraping.
- **Code location:** dead-db/lore/ subdirectory in this repo. MCP tools
  live in dead_mcp/tools.py (formerly homelab-mcp/tools/deaddb.py).
- **Schema split:** plain DDL in schema.sql, vec0 virtual table created
  in db.py (requires sqlite-vec extension loaded first).
- **Hard fail on model mismatch:** meta table records the embedding
  model + dim; init_schema raises if config disagrees with what's in
  the DB. Mismatched vectors silently produce wrong retrieval.
- **Source caps in router:** per-source chunk caps per query — lia_essays 3,
  wikipedia 2, book 4, deadcast 4, reddit 4; unlisted sources default 3; plus
  a per-doc cap of 2. Reddit also gets a SOURCE_WEIGHT=0.85 distance multiplier
  (boost) so first-person fan accounts surface above their raw vector rank.
- **Hybrid retrieval:** entity filter (dates/songs/era) applied as hard
  WHERE when entities are extracted; pure vector fallback otherwise.

## MCP tool inventory (16 total)

Served by **dead-mcp** at https://dead-mcp.quickswoodcapital.com/mcp (port 8768, Docker on arrstack).
All tools live in `dead_mcp/tools.py`.

### Setlist/archive tools
dead_stats, dead_setlist, dead_song_history, dead_shows, dead_plex_library,
dead_show_recordings, dead_this_date, dead_song_stats, dead_segues,
dead_run, dead_rare_songs

### Lore tools
dead_lore — raw semantic search, optional source filter
dead_ask  — entity-aware router: extracts dates/songs/era, hybrid retrieval,
            returns chunks + suggested SQL followup calls

### Community votes + listen-link tools
dead_best_versions — ranked distinct shows for a song or segue ("Scarlet > Fire"),
                     each with a Plexamp deep link (if owned) or best archive.org
                     recording (Charlie Miller preferred). Backed by plex_albums.guid.
dead_top_versions  — raw HeadyVersion votes per show for a song; returns archive_id.
                     Fan-out bug fixed (GROUP BY show_date, correlated archive subquery).
dead_show_votes    — all HV submissions for a date, sorted by vote score

### plex_albums schema note
`plex_albums` now has `guid TEXT` and `parent_guid TEXT` columns (bare IDs, e.g.
`5d07cc7f403c640290e8646b`), backfilled by running `plex.py`. These are used by
`dead_best_versions` to build Plexamp links without any live Plex network call.
`guid` is NULL for unmatched fan transfers (local:// items). Re-run `plex.py` after
any `build_db.py` rebuild to restore these columns.

`PLEX_MACHINE_ID` in `dead_mcp/tools.py` is the server's stable `machineIdentifier`.
If the Plex server is ever migrated to new hardware, update this constant and re-run
`plex.py` to refresh the guids.

### plex_tracks schema note
`plex_tracks` holds per-track guid + position data, backfilled by `plex.py` alongside
`plex_albums`. Scope is **owned ∩ HeadyVersion-voted** shows only
(`plex_albums.show_date ∩ community_votes.show_date`) — currently 389 albums / 8247
tracks. Widen to all owned albums by removing the voted filter in `plex.py:sync()` if
other tools need full track coverage.

**Why track-level links are required:** box-set and combo-release albums (Pacific
Northwest 73-74, Europe '72, Dick's Picks 28/29, etc.) share a single `plex://album/`
guid across every member show. An album-level Plexamp link (`/album/<guid>`) collapses
onto the box, not the individual show. Per-show uniqueness lives at the track level —
each track has a distinct `plex://track/` guid. `dead_best_versions` links at the track
level for all owned shows (not just box sets), landing on the queried song's actual
performance. Falls back to the show's first track if no title match; falls back to
archive.org (never an album link) if the album has no track rows in `plex_tracks`.

Track link format:
```
https://listen.plex.tv/track/<TRACK_GUID>
  ?source=<MACHINE_ID>
  &key=%2Flibrary%2Fmetadata%2F<TRACK_RK>
  &parentGuid=<ALBUM_GUID>
  &grandparentGuid=<ARTIST_GUID>
  &accountID=202609&username=BearsWorld
```
`PLEX_ACCOUNT_ID`/`PLEX_USERNAME` constants in `tools.py` match the validated links;
they're optional for playback but included to byte-match the known-good URLs.

## Repo layout (current)
```
dead-db/
  README.md
  CLAUDE.md                  # this file
  build_db.py                # phase 1: gdshowsdb YAML -> shows/performances
  plex.py                    # phase 1: Plex library -> plex_albums
  scrape_archive.py          # phase 4: archive.org cursor scrape
  build_archive.py           # phase 4: scrape -> archive_recordings
  build_headyversion.py      # phase 3 addendum: HV scraper -> community_votes in dead.db
  build_playdead.py          # phase A: nugs help-desk page -> playdead_shows/albums tables
  SPEC_playdead.md           # phase A spec (complete)
  requirements.txt
  unresolved_titles.log      # 119 Plex albums without a dateable title (expected)
  playdead_unresolved.log    # 5 shows not in gdshowsdb (all legit two-show days)
  dead_mcp/                  # phase 5: dedicated MCP server for all 16 dead tools
    __init__.py
    server.py                # FastMCP entry point, port 8768, OAuth 2.1
    oauth_provider.py        # auto-approving OAuth provider
    tools.py                 # all 16 dead tools (moved from homelab-mcp/tools/deaddb.py)
    requirements.txt         # includes torch + sentence-transformers
    Dockerfile               # python:3.12-slim, bind-mounted at /app in compose
  lore/                      # phase 3: RAG lore pipeline
    SPEC.md                  # scaffolding spec (locked, complete, historical)
    SPEC_wikipedia.md        # wikipedia fetcher spec (complete)
    SPEC_lia.md              # LIA fetcher spec (complete)
    SPEC_books.md            # books fetcher spec (complete)
    SPEC_song_matching.md    # song matching spec (complete)
    SPEC_mcp_tools.md        # MCP tools spec (complete)
    SPEC_headyversion.md       # HeadyVersion ingest spec (complete)
    SPEC_deadcast.md           # Deadcast fetcher spec (complete)
    SPEC_deadcast_DEFERRED.md  # old deferred spec (superseded, historical)
    SPEC_reddit.md             # Reddit corpus spec (Arctic Shift dump approach)
    SPEC_reddit_api.md         # parked live-API spec (Reddit now 403s unauth)
    build_lore_db.py           # orchestrator: idempotent ingest at source_id grain
    build_wikipedia.py         # Wikipedia corpus build -> dead_lore.db
    build_lia.py               # Light Into Ashes corpus build -> dead_lore.db
    build_books.py             # EPUB books corpus build -> dead_lore.db
    build_headyversion_lore.py # lore-path build for HV blurbs -> dead_lore.db
    build_deadcast.py          # Deadcast corpus build -> dead_lore.db
    build_reddit.py            # Reddit corpus build -> dead_lore.db
    headyversion_alias_proposals.txt  # HV->canonical alias proposals (human-reviewed)
    config.py
    schema.sql
    db.py
    embed.py
    normalize.py
    query.py
    router.py
    song_matcher.py
    match_songs.py
    smoke_test.py            # 16/16 passing
    articles.txt             # curated Wikipedia article list
    song_aliases.txt         # song name aliases for fuzzy matching
    song_stopwords.txt       # stopwords for song matching
    data/
      reddit/                # Arctic Shift dumps (not committed — large files)
        r_gratefuldead_posts.jsonl
        r_gratefuldead_comments.jsonl
    fetchers/
      __init__.py
      _base.py
      _html.py
      lia.py
      wikipedia.py
      books.py
      headyversion.py          # HV scraper (used by both build paths)
      deadcast.py              # Deadcast local-HTML fetcher
      reddit.py                # Arctic Shift offline dump fetcher
      reddit_api.py            # parked: live unauthenticated .json (now blocked)
```

### community_votes table (dead.db)

Populated by `build_headyversion.py`. Upserts on submission_id — safe to re-run.

```
community_votes(
    submission_id  INTEGER PK,
    heady_song_id  INTEGER,      -- HV internal ID
    song_uuid      TEXT,         -- NULL for unresolved/medley names
    song_name      TEXT,         -- HV display name (html-unescaped)
    show_date      TEXT,         -- ISO YYYY-MM-DD
    venue          TEXT,         -- NULL; sourced via JOIN to shows at query time
    city           TEXT,         -- NULL; ditto
    vote_score     INTEGER,
    blurb          TEXT,
    heady_url      TEXT,
    fetched_at     TEXT
)
```

Key build decisions:
- `fetch_show_meta=False` — venue/city/archive_id come from JOIN, not HTTP
- Segue names (`A -> B`) resolved to first-song UUID via post-upsert pass
- Medley names with `&` left unresolved (logged to `lore/headyversion_medleys_skipped.log`)
- Rebuild: `python3 -m build_headyversion` (~25 min). If build_db.py wipes dead.db, re-run this.

### Adding Deadcast episodes

1. Save the dead.net transcript page (File → Save Page As → Web Page, Complete)
   into `/hddpool/datastore/mediacenter/Audio/Deadcast/` on the NAS.
   The browser creates `<name>.html` + `<name>_files/` — both are fine; only
   the `.html` is read. macOS `._*` AppleDouble files are ignored automatically.
2. On arrstack:
   ```bash
   cd /home/bear/dead-db
   python3 -m lore.build_deadcast
   ```
   `ingest()` is idempotent on the canonical URL — existing episodes upsert
   cleanly, the new one is added. No dedup or cleanup needed.
3. Retrieval via `dead_lore`/`dead_ask` picks up the new chunks immediately
   (no MCP restart required — the DB is read at query time).

## How phase work happens

The pattern that's working:

1. Architecture conversation in Claude.ai → opinionated design choices.
2. Detailed SPEC.md committed to the repo (locked design, file layout,
   exact code, success criteria, explicit out-of-scope list).
3. Claude Code on arrstack reads SPEC.md and implements verbatim.
   "Stop and ask before improvising" is the instruction.
4. Smoke test or validation floor proves the contract.
5. Diff review, manual commit.

Resist the urge to skip step 2 for "small" phases. The spec is what
prevents drift; small phases drift hardest because they feel safe.

## Building / refreshing the Reddit corpus

Dumps live in `lore/data/reddit/` (not committed — too large). Download from
`arctic-shift.photon-reddit.com/download-tool`, select r/gratefuldead, grab
both posts and comments dumps. Drop them in the directory and run:

```bash
cd /home/bear/dead-db
python3 -m lore.build_reddit
```

Ingest is idempotent on permalink — safe to re-run after a fresh dump download.
The build streams the full comments file (~4.7 GB) so it takes a few minutes.

## What's next

Phases 1–5 + Phase A complete. Active next work:

- **Phase B** (`SPEC_playdead_links.md`, not yet written) — populate
  `playdead_shows.release_id` / `web_url` with `vault.playdead.app/release/<id>`
  deep links. Probe order: (1) try sitemap/public listing unauthenticated; (2) fall
  back to authenticated nugs catalog API. Then wire `dead_show_recordings` to show
  a "streaming on Play Dead" line. See SPEC_playdead.md §Phase B for details.

Maintenance / possible future work:
- Refresh Play Dead catalog: re-run `build_playdead.py` (page updated ~2 shows/Tuesday)
- Refresh Reddit dumps: re-download from Arctic Shift and re-run `build_reddit`
- Add more Deadcast episodes as they're saved (see workflow above)
- Refresh HeadyVersion votes: `python3 -m build_headyversion` (~25 min)
- Tune chunk size or swap embedding model if retrieval quality degrades
- Reddit corpus skews recent: the score gates (post>=10, comment>=5) filter out
  most pre-~2015 content because the subreddit was tiny then and good posts
  scored low. If older-era (70s/80s) coverage feels thin, swap the absolute
  score floor in `fetchers/reddit.py` for a relative threshold (e.g.
  `upvote_ratio`, present in the dumps, or a per-year percentile). Also note the
  newest ~36h of any fresh dump score 0-1 (Arctic Shift "archived too fresh")
  and are dropped by the gate — trivial slice, expected.
- Add more books to the EPUB library and re-run `lore/build_lore_db.py`
