# dead-db — project status

This file is the durable state of the dead-db project. Read it first when
opening this repo in a new session. It captures what's done, what's next,
and which design decisions are locked (so they're not relitigated).

## What this project is

A normalized SQLite database of Grateful Dead setlists, joined to my Plex
music library and archive.org recordings, with structured questions
answered by SQL and lore/insight questions answered by RAG. Exposed as
MCP tools via homelab-mcp.

See README.md for end-user-facing description. This file is for picking
up development between sessions.

## Phase status

- [x] **Phase 1** — date-keyed `shows` + `plex_albums` joined on date
- [x] **Phase 2** — setlist/stats query engine + 11 MCP tools in
      homelab-mcp/tools/deaddb.py
- [x] **Phase 4** — archive.org gap-fill (built ahead of phase 3 because
      archive.org has a clean API; no regrets)
- [~] **Phase 3** — RAG over Grateful Dead lore (in progress)
  - [x] Scaffolding (`lore/` package, smoke_test passes)
  - [ ] Wikipedia fetcher (NEXT)
  - [ ] Light Into Ashes fetcher (deadessays.blogspot)
  - [ ] Deadcast fetcher — first confirm transcripts exist or need Whisper
  - [ ] Song-name matching to populate chunks.mentioned_songs
  - [ ] MCP tools: dead_lore(query) + dead_ask(question) router

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
  will land in homelab-mcp/tools/deaddb.py alongside existing dead tools.
- **Schema split:** plain DDL in schema.sql, vec0 virtual table created
  in db.py (requires sqlite-vec extension loaded first).
- **Hard fail on model mismatch:** meta table records the embedding
  model + dim; init_schema raises if config disagrees with what's in
  the DB. Mismatched vectors silently produce wrong retrieval.

## Phase 3 next: Wikipedia fetcher

The reason Wikipedia goes before LIA: clean API, no rate-limit dance, no
HTML parsing. It validates the fetcher pattern and exposes real chunking
quality on real text — both signals you want before committing to LIA's
Blogspot scrape.

Open questions for that spec:
- Curated article list or category crawl? (Leaning curated — ~200-400
  hand-picked articles: every studio album, every member, key shows,
  every tour, key songs. Quality over coverage.)
- Plain text via Wikipedia API's `extracts` endpoint, or full wikitext
  parsed with mwparserfromhell? (Extracts is simpler; wikitext keeps
  structure for better chunking.)
- How to handle disambiguation pages and stubs? (Skip stubs; resolve
  disambiguation pages by picking the band-context article.)

## Repo layout (current)

```
dead-db/
  README.md
  CLAUDE.md                # this file
  SPEC.md                  # archived — moved to lore/SPEC.md after phase 3 scaffolding
  build_db.py              # phase 1: gdshowsdb YAML -> shows/performances
  plex.py                  # phase 1: Plex library -> plex_albums
  scrape_archive.py        # phase 4: archive.org cursor scrape
  build_archive.py         # phase 4: scrape -> archive_recordings
  requirements.txt
  unresolved_titles.log    # the 119 Plex albums without a dateable title
  lore/                    # phase 3
    SPEC.md                # phase 3 scaffolding spec (locked, complete)
    config.py              # EMBEDDING_MODEL, EMBEDDING_DIM, paths
    schema.sql             # documents, chunks, meta tables
    db.py                  # connect() + init_schema() with sqlite-vec
    embed.py               # bge-small wrapper, lazy-loaded
    normalize.py           # RawDocument -> Chunk (paragraph merge, ~512 tokens)
    build_lore_db.py       # orchestrator (idempotent at source_id grain)
    query.py               # search(query, k) -> ChunkResult
    smoke_test.py          # passes: OK: 3 docs, 3 chunks, Cornell top
    fetchers/
      _base.py             # Fetcher ABC, RawDocument dataclass
```

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
