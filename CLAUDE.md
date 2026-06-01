# dead-db — project status

This file is the durable state of the dead-db project. Read it first when
opening this repo in a new session. It captures what's done, what's next,
and which design decisions are locked (so they're not relitigated).

## What this project is

A normalized SQLite database of Grateful Dead setlists, joined to my Plex
music library and archive.org recordings, with structured questions
answered by SQL and lore/insight questions answered by RAG. Exposed as
15 MCP tools via homelab-mcp.

See README.md for end-user-facing description. This file is for picking
up development between sessions.

## Phase status

- [x] **Phase 1** — date-keyed `shows` + `plex_albums` joined on date
- [x] **Phase 2** — setlist/stats query engine + 11 MCP tools in
      homelab-mcp/tools/deaddb.py
- [x] **Phase 4** — archive.org gap-fill (built ahead of phase 3 because
      archive.org has a clean API; no regrets)
- [x] **Phase 3** — RAG over Grateful Dead lore
  - [x] Scaffolding (`lore/` package, smoke_test 16/16 passing)
  - [x] Wikipedia fetcher (`lore/fetchers/wikipedia.py`, ~110 curated articles)
  - [x] Light Into Ashes fetcher (`lore/fetchers/lia.py`, ~200 essays + primary sources)
  - [x] Books fetcher (`lore/fetchers/books.py`, EPUB library)
  - [x] Song-name matching (`lore/song_matcher.py`, `lore/match_songs.py`)
  - [x] MCP tools: `dead_lore` + `dead_ask` in homelab-mcp/tools/deaddb.py
  - [x] Router (`lore/router.py`) — entity extraction + hybrid retrieval + followup suggestions
  - [x] HeadyVersion ingest (`build_headyversion.py`, `lore/fetchers/headyversion.py`)
        25,012 community votes in `dead.db.community_votes`; 93% resolved to song_uuid;
        segue entries (China>Rider etc.) mapped to first-song UUID.
        MCP tools: `dead_top_versions` + `dead_show_votes`.
  - [x] Deadcast fetcher (`lore/fetchers/deadcast.py`, `lore/build_deadcast.py`)
        Local-HTML path — saved dead.net transcript pages on NAS. No network, no Whisper.
        10 episodes ingested (8 WD50 + 2 BONUS), 320 chunks. Idempotent; grows by
        dropping more saved pages into DEADCAST_DIR and re-running build_deadcast.

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
  live in homelab-mcp/tools/deaddb.py alongside existing dead tools.
- **Schema split:** plain DDL in schema.sql, vec0 virtual table created
  in db.py (requires sqlite-vec extension loaded first).
- **Hard fail on model mismatch:** meta table records the embedding
  model + dim; init_schema raises if config disagrees with what's in
  the DB. Mismatched vectors silently produce wrong retrieval.
- **Source caps in router:** per-source chunk caps per query — lia_essays 3,
  wikipedia 2, book 4, deadcast 4; unlisted sources default 3; plus a per-doc
  cap of 2. Prevents any single corpus dominating results.
- **Hybrid retrieval:** entity filter (dates/songs/era) applied as hard
  WHERE when entities are extracted; pure vector fallback otherwise.

## MCP tool inventory (15 total)

### Setlist/archive tools (homelab-mcp/tools/deaddb.py)
dead_stats, dead_setlist, dead_song_history, dead_shows, dead_plex_library,
dead_show_recordings, dead_this_date, dead_song_stats, dead_segues,
dead_run, dead_rare_songs

### Lore tools (homelab-mcp/tools/deaddb.py)
dead_lore — raw semantic search, optional source filter
dead_ask  — entity-aware router: extracts dates/songs/era, hybrid retrieval,
            returns chunks + suggested SQL followup calls

### Community votes tools (homelab-mcp/tools/deaddb.py)
dead_top_versions — top HeadyVersion-voted performances of a song; JOINs
                    shows + archive_recordings for venue/city/archive_id
dead_show_votes   — all HV submissions for a date, sorted by vote score

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
  requirements.txt
  unresolved_titles.log      # 119 Plex albums without a dateable title (expected)
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
    build_headyversion_lore.py # lore-path build for HV blurbs -> dead_lore.db
    build_deadcast.py          # Deadcast corpus build -> dead_lore.db
    headyversion_alias_proposals.txt  # HV->canonical alias proposals (human-reviewed)
    config.py
    schema.sql
    db.py
    embed.py
    normalize.py
    build_lore_db.py
    query.py
    router.py
    song_matcher.py
    match_songs.py
    smoke_test.py            # 16/16 passing
    articles.txt             # curated Wikipedia article list
    song_aliases.txt         # song name aliases for fuzzy matching
    song_stopwords.txt       # stopwords for song matching
    fetchers/
      _base.py
      _html.py
      lia.py
      wikipedia.py
      books.py
      headyversion.py          # HV scraper (used by both build paths)
      deadcast.py              # Deadcast local-HTML fetcher
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

## What's next

Phase 3 is complete. Project is feature-complete.

Possible future work:
- Add more Deadcast episodes as they're saved (see workflow above)
- Refresh HeadyVersion votes: `python3 -m build_headyversion` (~25 min)
- Tune chunk size or swap embedding model if retrieval quality degrades
- Add more books to the EPUB library and re-run `lore/build_lore_db.py`
