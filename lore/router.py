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
# Per-source chunk caps for a single answer. Sources not listed fall back to
# PER_SOURCE_CAP_DEFAULT. Prevents any one corpus dominating an answer.
PER_SOURCE_CAP_DEFAULT = 3
PER_SOURCE_CAP = {
    "lia_essays": 3,
    "wikipedia": 2,
    "book": 4,
    "deadcast": 4,
    "reddit": 4,        # top tier: may contribute up to 4 chunks per answer
}

# Multiplier on vector distance per source. <1 boosts (smaller effective
# distance ranks higher); 1.0 is neutral. Reddit is weighted up so first-person
# accounts surface — but only modestly; it is the noisiest source.
SOURCE_WEIGHT_DEFAULT = 1.0
SOURCE_WEIGHT = {
    "reddit": 0.85,
}


def _apply_caps(rows: list[RouterChunk], k: int) -> list[RouterChunk]:
    """Diversity caps: max PER_DOC_CAP chunks/doc, per-source cap chunks/source."""
    by_doc: dict[str, int] = {}
    by_src: dict[str, int] = {}
    out: list[RouterChunk] = []
    for r in rows:
        doc_key = f"{r.source}::{r.title}"
        src_cap = PER_SOURCE_CAP.get(r.source, PER_SOURCE_CAP_DEFAULT)
        if by_doc.get(doc_key, 0) >= PER_DOC_CAP:
            continue
        if by_src.get(r.source, 0) >= src_cap:
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
    chunks.sort(key=lambda c: c.distance * SOURCE_WEIGHT.get(c.source, SOURCE_WEIGHT_DEFAULT))
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
_FOLLOWUP_BY_SONG = [
    ("dead_song_history", lambda s: {"song": s}, "Every performance of this song"),
    ("dead_song_stats",   lambda s: {"song": s}, "Statistics for this song"),
    ("dead_top_versions", lambda s: {"song_name": s},
     "Top community-voted versions of this song from HeadyVersion"),
]
_FOLLOWUP_BY_DATE = [
    ("dead_setlist",         lambda d: {"date": d},  "Question mentions a specific show date"),
    ("dead_show_recordings", lambda d: {"date": d},  "Show how to hear this show"),
    ("dead_run",             lambda d: {"date": d},  "Adjacent shows give tour context"),
    ("dead_show_votes",      lambda d: {"date": d},  "Community-voted submissions from this show"),
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
