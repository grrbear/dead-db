# dead-db phase 3 — MCP tools (final phase)

## Context

The final phase 3 piece. Surfaces everything built so far through two new
MCP tools that join the existing 11 in `homelab-mcp/tools/deaddb.py`:

- **`dead_lore(query, k, ...)`** — raw semantic search over `dead_lore.db`.
  Thin wrapper around `lore.query.search()`. Returns top-k chunks +
  metadata. Useful for "find me chunks about X" style use.

- **`dead_ask(question)`** — the lore router. Extracts entities from the
  question (dates, songs, eras), runs hybrid retrieval (entity pre-filters
  + vector search) over `dead_lore.db`, AND returns suggested followup SQL
  tool calls. Structured evidence, not synthesized prose — the chat-Claude
  that called the tool synthesizes the final answer.

Read `lore/SPEC.md`, all four corpus specs, and `lore/SPEC_song_matching.md`
before this file. This spec assumes all are implemented and committed and
that `chunks.mentioned_songs` is populated.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db; raw_text
in documents; section hints; chunks.section + chunks.mentioned_songs +
chunks.mentioned_dates + chunks.era; documents.metadata JSON; stdlib +
sentence-transformers + sqlite-vec only. See prior specs.

## New decisions for this phase

- **Structured evidence, not LLM synthesis inside the tool.** `dead_ask`
  returns chunks + entities + followup suggestions as data. The chat-Claude
  writes the prose. No internal LLM call, no API key, no model selection,
  no synthesis logic.
- **Hybrid retrieval, not pure vector.** When entities are extractable from
  the question (dates, songs, era), they hard-filter the chunk pool BEFORE
  vector ranking. Pure vector fallback when no entities found.
- **Followup-tool suggestions ON BY DEFAULT.** `dead_ask` returns a
  `suggested_followups` list naming existing SQL tools and their arguments,
  derived from extracted entities. This is the router's main value-add
  beyond gathering chunks — telling the chat-Claude which of the 11 phase-2
  SQL tools to call next.
- **Strict per-source caps in `dead_ask` results.** Without caps a single
  source (say, McNally's book) can dominate the top-k. Cap at 2 chunks per
  document and 3 per source value, before final top-k truncation.
- **Confidence-aware song handling.** `dead_ask` uses song matches with
  `confidence >= 0.6` for entity extraction (high precision). The full
  `mentioned_songs` JSON on each returned chunk includes all matches
  regardless of confidence (the chat-Claude can see the full picture).
- **No new MCP tools beyond these two.** Resist adding `dead_lore_admin`,
  `dead_lore_stats`, etc. Two tools, sharp definitions.

---

## File layout

```
dead-db/
  lore/
    router.py            # NEW — entity extraction + hybrid retrieval + followup hints
    ...                  # existing files unchanged

homelab-mcp/
  tools/
    deaddb.py            # MODIFIED — adds dead_lore + dead_ask
```

`router.py` is the bulk of the new code. The MCP tool functions in
`deaddb.py` are thin wrappers — they validate inputs, call into
`lore.router`, format outputs as MCP-tool responses.

---

## `lore/router.py` — the router

```python
"""Entity extraction + hybrid retrieval + followup-tool suggestions for dead_ask.

The router's three jobs:
  1. Extract dates, songs, and era hints from a free-text question.
  2. Run hybrid retrieval over dead_lore.db: hard filter by entities when
     present, vector rank within the filtered set, fall back to pure vector
     when no entities found.
  3. Suggest which of the 11 existing phase-2 SQL tools to call next, with
     concrete arguments derived from the extracted entities.

Does NOT call the SQL tools itself. Does NOT synthesize prose. Pure data
gathering + structured suggestions for the chat-Claude.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, asdict, field

from .config import LORE_DB_PATH
from .db import connect
from .embed import embed
from .song_matcher import match_songs

# ---------- entity extraction ----------

DATE_RE = re.compile(r"\b(19[6-9]\d|20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
# loose date forms: 5/8/77, 5/8/1977, May 8 1977
LOOSE_DATE_RE = re.compile(
    r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b"
)
YEAR_RE = re.compile(r"\b(19[6-9]\d|20[01]\d)\b")   # 1960-2019 plausible band-era years

# era boundaries (must match normalize._era_for)
ERA_BOUNDS = [
    (1965, 1971, "pigpen"),
    (1972, 1978, "keith"),
    (1979, 1989, "brent"),
    (1990, 1995, "bruce"),
]

MIN_SONG_CONFIDENCE_FOR_ENTITY = 0.6


@dataclass
class ExtractedEntities:
    dates: list[str] = field(default_factory=list)       # ISO YYYY-MM-DD
    years: list[int] = field(default_factory=list)       # bare years from text
    songs: list[str] = field(default_factory=list)       # canonical names, conf>=0.6
    eras: list[str] = field(default_factory=list)        # 'pigpen'|'keith'|'brent'|'bruce'


def _normalize_loose_date(m: re.Match) -> str | None:
    """Convert 5/8/77 -> 1977-05-08. Returns ISO or None if out of band era."""
    a, b, c = m.group(1), m.group(2), m.group(3)
    mo, d, y = int(a), int(b), int(c)
    if y < 100:
        y = 1900 + y if y >= 60 else 2000 + y
    if not (1965 <= y <= 2019) or not (1 <= mo <= 12) or not (1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def extract_entities(text: str) -> ExtractedEntities:
    ents = ExtractedEntities()

    iso = set(m.group(0) for m in DATE_RE.finditer(text))
    for m in LOOSE_DATE_RE.finditer(text):
        n = _normalize_loose_date(m)
        if n:
            iso.add(n)
    ents.dates = sorted(iso)

    bare_years = {int(m.group(0)) for m in YEAR_RE.finditer(text)}
    # only keep bare years NOT already part of an ISO date we captured
    iso_years = {int(d[:4]) for d in ents.dates}
    ents.years = sorted(bare_years - iso_years)

    # songs via the matcher; only high-confidence ones become entities
    song_hits = match_songs(text)
    ents.songs = sorted({h.name for h in song_hits
                         if h.confidence >= MIN_SONG_CONFIDENCE_FOR_ENTITY})

    # eras: from any date/year in the question
    era_years = iso_years | set(ents.years)
    eras = set()
    for y in era_years:
        for lo, hi, era in ERA_BOUNDS:
            if lo <= y <= hi:
                eras.add(era)
                break
    ents.eras = sorted(eras)

    return ents


# ---------- hybrid retrieval ----------

@dataclass
class RouterChunk:
    chunk_id: int
    distance: float
    text: str
    source: str
    title: str
    url: str
    section: str | None
    mentioned_dates: list[str]
    mentioned_songs: list[dict]
    era: str | None
    document_metadata: dict | None


def _entity_filter_sql(ents: ExtractedEntities) -> tuple[str, list]:
    """Build a SQL WHERE clause matching chunks that mention ANY extracted entity.

    Disjunction across entity kinds (date OR song OR era), because the user's
    question often has just one. Returns (clause, params) suitable for
    appending to a SELECT.
    """
    clauses, params = [], []

    for d in ents.dates:
        clauses.append("c.mentioned_dates LIKE ?")
        params.append(f"%{d}%")

    for s in ents.songs:
        # mentioned_songs is JSON like [{"name":"X",...}, ...]; match the name field
        clauses.append("c.mentioned_songs LIKE ?")
        params.append(f'%"name": "{s}"%')

    for era in ents.eras:
        clauses.append("c.era = ?")
        params.append(era)

    if not clauses:
        return "", []
    return "(" + " OR ".join(clauses) + ")", params


PER_DOC_CAP = 2
PER_SOURCE_CAP = 3


def _apply_caps(rows: list[RouterChunk], k: int) -> list[RouterChunk]:
    """Diversity caps: max PER_DOC_CAP chunks/doc, PER_SOURCE_CAP chunks/source."""
    by_doc: dict[str, int] = {}
    by_src: dict[str, int] = {}
    out: list[RouterChunk] = []
    for r in rows:
        doc_key = f"{r.source}::{r.title}"
        if by_doc.get(doc_key, 0) >= PER_DOC_CAP:
            continue
        if by_src.get(r.source, 0) >= PER_SOURCE_CAP:
            continue
        out.append(r)
        by_doc[doc_key] = by_doc.get(doc_key, 0) + 1
        by_src[r.source] = by_src.get(r.source, 0) + 1
        if len(out) >= k:
            break
    return out


def retrieve(question: str, *, k: int = 8,
             db_path: str = LORE_DB_PATH) -> tuple[list[RouterChunk], ExtractedEntities]:
    """Hybrid retrieval. Returns (capped top-k, entities)."""
    ents = extract_entities(question)
    qvec = embed([question])[0].tobytes()

    conn = connect(db_path, readonly=True)
    cur = conn.cursor()

    where_entity, params_entity = _entity_filter_sql(ents)
    # over-fetch from vec0 then filter/rank, so caps still leave us at k.
    overfetch = max(k * 8, 40)

    if where_entity:
        # filter THEN rank by vector distance within the filtered set
        sql = f"""
          WITH filtered AS (
            SELECT c.id FROM chunks c WHERE {where_entity}
          )
          SELECT v.chunk_id, v.distance,
                 c.text, c.section, c.mentioned_dates, c.mentioned_songs, c.era,
                 d.source, d.title, d.url, d.metadata
          FROM chunk_vectors v
          JOIN filtered f  ON f.id = v.chunk_id
          JOIN chunks c    ON c.id = v.chunk_id
          JOIN documents d ON d.id = c.document_id
          WHERE v.embedding MATCH ? AND k = ?
          ORDER BY v.distance
          LIMIT ?
        """
        rows = cur.execute(sql, [*params_entity, qvec, overfetch, overfetch]).fetchall()
        # graceful degradation: if hard filter returned too few results,
        # backfill with pure-vector hits
        if len(rows) < k:
            sql_fb = """
              SELECT v.chunk_id, v.distance,
                     c.text, c.section, c.mentioned_dates, c.mentioned_songs, c.era,
                     d.source, d.title, d.url, d.metadata
              FROM chunk_vectors v
              JOIN chunks c    ON c.id = v.chunk_id
              JOIN documents d ON d.id = c.document_id
              WHERE v.embedding MATCH ? AND k = ?
              ORDER BY v.distance LIMIT ?
            """
            seen = {r[0] for r in rows}
            extra = cur.execute(sql_fb, [qvec, overfetch, overfetch]).fetchall()
            rows.extend(r for r in extra if r[0] not in seen)
    else:
        # pure vector path
        sql = """
          SELECT v.chunk_id, v.distance,
                 c.text, c.section, c.mentioned_dates, c.mentioned_songs, c.era,
                 d.source, d.title, d.url, d.metadata
          FROM chunk_vectors v
          JOIN chunks c    ON c.id = v.chunk_id
          JOIN documents d ON d.id = c.document_id
          WHERE v.embedding MATCH ? AND k = ?
          ORDER BY v.distance LIMIT ?
        """
        rows = cur.execute(sql, [qvec, overfetch, overfetch]).fetchall()

    conn.close()

    chunks = [
        RouterChunk(
            chunk_id=r[0], distance=r[1], text=r[2], section=r[3],
            mentioned_dates=json.loads(r[4] or "[]"),
            mentioned_songs=json.loads(r[5] or "[]"),
            era=r[6], source=r[7], title=r[8], url=r[9],
            document_metadata=json.loads(r[10]) if r[10] else None,
        ) for r in rows
    ]
    return _apply_caps(chunks, k), ents


# ---------- followup suggestions ----------

@dataclass
class FollowupSuggestion:
    tool: str
    args: dict
    reason: str


# entity-kind -> [(tool_name, arg_builder, reason)]
# arg_builder is a lambda(entity_value) -> dict.
# Tool names match existing phase-2 MCP tools in homelab-mcp/tools/deaddb.py.
_FOLLOWUP_BY_DATE = [
    ("dead_setlist",        lambda d: {"date": d},  "Question mentions a specific show date"),
    ("dead_show_recordings", lambda d: {"date": d}, "Show how to hear this show"),
    ("dead_run",            lambda d: {"date": d},  "Adjacent shows give tour context"),
]
_FOLLOWUP_BY_SONG = [
    ("dead_song_history", lambda s: {"name": s}, "Every performance of this song"),
    ("dead_song_stats",   lambda s: {"name": s}, "Statistics for this song"),
]
_FOLLOWUP_BY_YEAR = [
    ("dead_shows", lambda y: {"year": y}, "All shows in this year"),
]


def followup_suggestions(ents: ExtractedEntities) -> list[FollowupSuggestion]:
    out: list[FollowupSuggestion] = []
    for d in ents.dates:
        for tool, build, reason in _FOLLOWUP_BY_DATE:
            out.append(FollowupSuggestion(tool=tool, args=build(d), reason=reason))
    for s in ents.songs:
        for tool, build, reason in _FOLLOWUP_BY_SONG:
            out.append(FollowupSuggestion(tool=tool, args=build(s), reason=reason))
    # only suggest year-based shows if no specific date was extracted
    if not ents.dates:
        for y in ents.years:
            for tool, build, reason in _FOLLOWUP_BY_YEAR:
                out.append(FollowupSuggestion(tool=tool, args=build(y), reason=reason))
    return out


# ---------- public entry points ----------

def ask(question: str, *, k: int = 8,
        db_path: str = LORE_DB_PATH) -> dict:
    """Run the router. Returns a dict ready to serialize as MCP tool output."""
    chunks, ents = retrieve(question, k=k, db_path=db_path)
    suggestions = followup_suggestions(ents)
    return {
        "question": question,
        "entities": asdict(ents),
        "rag_chunks": [asdict(c) for c in chunks],
        "suggested_followups": [asdict(s) for s in suggestions],
        "retrieval_mode": "hybrid" if (ents.dates or ents.songs or ents.eras) else "pure_vector",
    }
```

Implementer notes:
- The `mentioned_songs LIKE '%"name": "X"%'` filter is deliberately tolerant
  — exact JSON serialization formats can vary. It matches the canonical
  name field regardless of confidence (the entity-extraction step already
  filtered for confidence). Do NOT switch to a JSON1-extension call; keep
  it portable.
- The hybrid path's backfill (when hard-filter returns too few rows) is
  important — without it, a question like "what's the best Cornell 77
  story" might return 2 chunks because only 2 chunks mention `1977-05-08`
  literally. The vector backfill gives the chat-Claude the context to still
  answer well.
- The per-doc/per-source caps run AFTER overfetch and AFTER ranking, so a
  single dominant source can't crowd out diversity but quality ranking is
  preserved.
- `extract_entities` reuses `match_songs` from the song-matching phase —
  the matcher's confidence filter (≥0.6) prevents low-quality song matches
  from triggering misleading followup suggestions.

---

## `homelab-mcp/tools/deaddb.py` — adding the two MCP tools

These functions get added alongside the existing 11 tools in the same
module, following the same MCP tool decorator pattern already in use. The
implementer should match the file's existing style (whatever decorator,
docstring shape, error handling, and JSON-return convention the other 11
tools use — DO NOT change the established style).

Skeleton of what to add:

```python
# ... existing imports, existing 11 tools ...

# add to the top-level imports section:
import sys
sys.path.insert(0, "/home/bear/dead-db")  # already on path for other tools; verify
from lore import query as lore_query
from lore.router import ask as router_ask


@<existing_mcp_tool_decorator>
def dead_lore(query: str, k: int = 5, source: str | None = None) -> dict:
    """Raw semantic search over the Grateful Dead lore corpus.

    Returns top-k chunks of prose from Wikipedia, Light Into Ashes essays
    and primary-source clippings, and Grateful Dead books, ranked by
    semantic similarity to the query.

    Args:
        query: Free-text search query.
        k: Number of chunks to return (default 5, max 20).
        source: Optional filter — one of 'wikipedia', 'lia_essays',
                'lia_sources', 'book'. Default None = all sources.

    Returns: {chunks: [{text, source, title, url, section, distance,
              mentioned_dates, mentioned_songs, era}]}
    """
    k = max(1, min(int(k), 20))
    results = lore_query.search(query, k=k, source=source)
    return {
        "chunks": [
            {
                "text": r.text,
                "source": r.source,
                "title": r.title,
                "url": r.url,
                "section": getattr(r, "section", None),
                "distance": r.distance,
                "mentioned_dates": r.mentioned_dates,
                "mentioned_songs": r.mentioned_songs,
                "era": r.era,
            }
            for r in results
        ]
    }


@<existing_mcp_tool_decorator>
def dead_ask(question: str, k: int = 8) -> dict:
    """Lore router for narrative/insight questions about the Grateful Dead.

    Extracts entities (dates, songs, eras) from the question, runs hybrid
    retrieval over the lore corpus (entity filters + vector ranking), and
    returns evidence chunks AND suggested followup SQL-tool calls.

    The CALLER (i.e. you, the chat assistant reading this docstring) is
    expected to:
      1. Read the rag_chunks for context.
      2. Make followup calls to the suggested tools (or others as needed)
         for structured data.
      3. Synthesize a final answer drawing on both.

    This tool does NOT synthesize prose. It gathers evidence and points at
    the next moves. Useful for any question whose answer combines lore
    (why/how/context) with structured facts (what/when/who).

    Args:
        question: A free-text question about the Grateful Dead.
        k: Number of chunks to return after diversity caps (default 8).

    Returns:
        {
          question: <echo>,
          entities: {dates, years, songs, eras},
          rag_chunks: [{chunk_id, distance, text, source, title, url,
                        section, mentioned_dates, mentioned_songs, era,
                        document_metadata}],
          suggested_followups: [{tool, args, reason}],
          retrieval_mode: 'hybrid' | 'pure_vector'
        }
    """
    k = max(1, min(int(k), 20))
    return router_ask(question, k=k)
```

Implementer notes:
- The `@<existing_mcp_tool_decorator>` placeholder MUST be replaced with
  whatever decorator the existing 11 tools use. DO NOT invent a new
  pattern. Look at the existing file and match style exactly.
- Verify `sys.path` already includes `/home/bear/dead-db` (the other dead
  tools depend on it). If yes, the explicit insert above is unnecessary —
  drop it. If no, leave the insert but lift it up next to the existing
  setup line.
- The docstring for `dead_ask` is intentionally addressed to "you, the
  chat assistant reading this docstring" — MCP tool docstrings ARE the
  instructions to the calling LLM. Phrase them to guide good calling
  behavior. Keep this phrasing.
- The k clamping (1..20) is a guardrail; don't widen it.

---

## Success criteria

1. `lore/router.py` exists with `extract_entities`, `retrieve`,
   `followup_suggestions`, and `ask`.
2. `homelab-mcp/tools/deaddb.py` has the two new tools (`dead_lore`,
   `dead_ask`) registered alongside the existing 11. Style matches the
   existing tools exactly.
3. The existing smoke test still passes unchanged
   (`python3 -m lore.smoke_test` -> "OK: 3 docs...").
4. Unit-style validation in a REPL — show me the output of each:
   ```python
   from lore.router import extract_entities, ask
   # entity extraction
   extract_entities("What's the deal with Cornell 5/8/77?")
   # expect: dates=['1977-05-08'], years=[], songs=[], eras=['keith']

   extract_entities("Tell me about the Scarlet > Fire from Cornell 77")
   # expect: dates=[], years=[1977], songs=['Scarlet Begonias',
   #                                        'Fire on the Mountain'], eras=['keith']

   extract_entities("What defines the Brent era?")
   # expect: dates=[], years=[], songs=[], eras=[]
   #   (we don't auto-extract "brent" from a word; eras only come from years/dates)

   # full router call (live against dead_lore.db)
   r = ask("What's the deal with Cornell 5/8/77?", k=4)
   # expect: retrieval_mode='hybrid', some rag_chunks present (Cornell 77
   #   has lore coverage in book + Wikipedia), suggested_followups names
   #   dead_setlist/dead_show_recordings/dead_run all with date='1977-05-08'.
   ```
5. End-to-end smoke from the MCP side: restart homelab-mcp, then from
   any Claude client connected to it, call `dead_lore(query="Wall of Sound
   engineering", k=3)` and `dead_ask(question="why is Cornell 5/8/77 special")`.
   Both must return without error and contain non-empty `chunks` /
   `rag_chunks` arrays. Show me the JSON.
6. Manual quality check on `dead_ask`:
   ```
   dead_ask("Tell me about the proto-Solomon Jam in 1972-73 Dark Stars")
   ```
   Verify:
   - At least one chunk surfaces from source='lia_essays' (LIA's
     Proto-Solomon Jam essay is literally on this topic)
   - retrieval_mode='hybrid' (years extracted)
   - No single source dominates (per-source cap respected)
7. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- LLM synthesis inside `dead_ask`. Settled: chat-Claude synthesizes.
- A `dead_lore_admin` tool, a `dead_lore_stats` tool, a `dead_lore_rebuild`
  tool. Two tools only.
- Caching / memoization of the embedding model across MCP-tool calls beyond
  what `lore.embed._model()`'s `@lru_cache` already does. The MCP server is
  long-running; the model loads once at first call and stays.
- New entity types beyond dates/years/songs/eras. Venues, members, albums
  would be reasonable additions but are explicitly future work — adding
  them changes the entity_filter SQL and the followup-suggestion table,
  which is a separate, scoped enhancement.
- Tuning of PER_DOC_CAP, PER_SOURCE_CAP, MIN_SONG_CONFIDENCE_FOR_ENTITY,
  or any retrieval-tunable. Ship with the defaults; tune from real use.
- Changes to the existing 11 phase-2 SQL tools.

---

## Notes for the implementer

- Read this file + lore/SPEC.md + lore/SPEC_song_matching.md before coding.
- The router is the bulk of the new code. The MCP tool wrappers in
  homelab-mcp are deliberately thin — preserve that.
- The `dead_ask` docstring is the calling LLM's instructions. Take care
  with the phrasing — it's not documentation, it's a prompt.
- Followup suggestions key off `_FOLLOWUP_BY_DATE` / `_FOLLOWUP_BY_SONG` /
  `_FOLLOWUP_BY_YEAR` tables. If you add a new SQL tool to homelab-mcp
  later, adding a one-line entry to the right table here surfaces it as a
  suggestion. This is the only maintenance surface for the router/SQL-tool
  coupling.
- Entity extraction is deliberately conservative. It's OK for entities to
  be missed — the pure-vector fallback handles those questions fine. It's
  NOT OK for entity extraction to be wrong (a bad date suggestion sends the
  chat-Claude calling dead_setlist with garbage args). Bias toward precision.
- The hybrid backfill (entity filter returns too few, top up from pure
  vector) is essential. Without it the router produces too-narrow result
  sets on common questions. Don't simplify it away.
- Match existing dead-db style: terse docstrings, stdlib over deps.
