> **Status: COMPLETE.** This spec describes the scaffolding phase only.
> All subsequent phase 3 work (fetchers, song matching, MCP tools, router)
> is documented in separate SPEC_*.md files in this directory.
> See CLAUDE.md for overall phase status.

# dead-db phase 3 — lore pipeline scaffolding

## Context

This is phase 3 of `dead-db`: a RAG layer for Grateful Dead lore that lives alongside the existing setlist DB. It adds semantic search over Grateful Dead prose corpora (Light Into Ashes, Wikipedia, Deadcast transcripts, more later) so the MCP tools can answer "why is Cornell '77 special" — questions whose answers aren't in any structured field.

- Sibling SQLite file at `/hddpool/datastore/dead_lore.db`, separate from `dead.db`.
- Joins to `dead.db` by `show_date` string (`YYYY-MM-DD`).
- CPU embedding on arrstack via `bge-small-en-v1.5` through `sentence-transformers`.
- Vector search via `sqlite-vec` loadable extension.

**This phase delivers the library + a passing smoke test only.** No fetchers, no MCP tools, no router. Those are explicitly separate phases (see "Out of scope" below).

---

## Design decisions — locked, do not redesign

These are the outputs of a long architecture conversation. Some of them will look arbitrary without that context; that's expected. If something here genuinely seems wrong, **stop and ask** — don't quietly change it.

- **Vector store: `sqlite-vec`.** Not Chroma, not Qdrant, not FAISS, not a Docker container. Single-file extension that loads into stock Python sqlite3, lives on the same ZFS dataset as `dead.db`.
- **Embedding model: `BAAI/bge-small-en-v1.5`** at 384 dimensions, CPU inference on arrstack. Not bge-base, not bge-large, not Voyage/OpenAI/Cohere API, not local LLM endpoints. Decision rationale: corpus is small (~1500-2500 chunks), indexing is rare, query latency budget is generous (MCP calls, not real-time), and the iGPU is reserved for Immich and future Whisper work.
- **Separate database file.** `dead_lore.db` is its own SQLite file at `/hddpool/datastore/dead_lore.db`. Not merged into `dead.db`. Rebuilds of one must not touch the other.
- **`raw_text` is stored in `documents`.** Trades ~50-200 MB of disk for the ability to re-chunk without re-scraping. Re-scraping is slow and impolite to source sites.
- **Code lives in `dead-db/lore/`.** Subdirectory of the existing `dead-db` repo. Not a new repo. Not in `homelab-mcp`. The MCP tools that will eventually surface this go in `homelab-mcp/tools/deaddb.py` like the other dead tools, but that's a later phase.
- **Schema split between `.sql` file and `db.py`.** Plain DDL goes in `schema.sql`. The `vec0` virtual table for vectors is created from Python in `db.py` because virtual-table DDL requires `sqlite-vec` to already be loaded — it can't live in a static `.sql` file.
- **Hard fail on model mismatch.** `meta` table records the embedding model + dimension. If config disagrees with what's in the DB, `init_schema` raises rather than silently corrupting the index with vectors from a different model.
- **CPU-only PyTorch wheel.** The `sentence-transformers` install will pull in `torch` (~800 MB). Use the CPU-only wheel. We don't have CUDA and we don't want CUDA.

---

## File layout

```
dead-db/
  lore/
    __init__.py
    SPEC.md                  # this file (move from repo root after creating lore/)
    config.py
    schema.sql
    db.py
    embed.py
    fetchers/
      __init__.py
      _base.py
    normalize.py
    build_lore_db.py
    query.py
    smoke_test.py
  requirements.txt           # ADD three lines; do not remove existing entries
```

`lore/__init__.py` and `lore/fetchers/__init__.py` are empty files (just so the package imports cleanly).

---

## Files

### `lore/config.py`

```python
"""Single source of truth for lore-pipeline configuration.

Centralized so a model swap is one line and the meta table can record what
produced the vectors. Env vars override defaults for testing.
"""
import os

EMBEDDING_MODEL = os.environ.get("LORE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = int(os.environ.get("LORE_EMBEDDING_DIM", "384"))
LORE_DB_PATH = os.environ.get("LORE_DB_PATH", "/hddpool/datastore/dead_lore.db")

# chunking — tokens, approximate (we count chars/4 as cheap proxy)
CHUNK_SIZE = int(os.environ.get("LORE_CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.environ.get("LORE_CHUNK_OVERLAP", "64"))
```

### `lore/schema.sql`

```sql
-- dead_lore.db schema. Sibling of dead.db; joins by show_date string.
-- Rebuild policy: build_lore_db.py is idempotent at (source, source_id) grain.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,        -- 'lia', 'wikipedia', 'deadcast'
    source_id   TEXT NOT NULL,        -- stable id within source (URL, slug)
    title       TEXT,
    url         TEXT,
    published   TEXT,                 -- ISO date if known
    fetched_at  TEXT NOT NULL,        -- ISO datetime of fetch
    raw_text    TEXT NOT NULL,        -- cleaned plain text, no HTML
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,    -- 0-based position within doc
    text            TEXT NOT NULL,
    -- bridges back to dead.db (extracted at normalize time):
    mentioned_dates TEXT,                -- JSON array of YYYY-MM-DD
    mentioned_songs TEXT,                -- JSON array of canonical song names
    era             TEXT,                -- '60s'|'pigpen'|'keith'|'brent'|'bruce'|NULL
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

-- Vector table is created in db.py after sqlite-vec is loaded; can't be in a
-- plain .sql file because the virtual-table syntax requires the extension.
```

### `lore/db.py`

```python
"""SQLite connection helper. Loads sqlite-vec and ensures schema exists."""
import sqlite3
import os
from pathlib import Path
import sqlite_vec  # pip install sqlite-vec

from .config import LORE_DB_PATH, EMBEDDING_MODEL, EMBEDDING_DIM

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str = LORE_DB_PATH, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded. Creates parent dir if needed."""
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql + create vector table. Idempotent.

    Hard-fails if the DB was built with a different embedding model than
    config currently specifies — mismatched vectors silently produce wrong
    retrieval, so we'd rather refuse to open than corrupt the index.
    """
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBEDDING_DIM}]
        )
    """)
    existing = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if existing.get("embedding_model") and existing["embedding_model"] != EMBEDDING_MODEL:
        raise RuntimeError(
            f"DB was built with {existing['embedding_model']} but config says "
            f"{EMBEDDING_MODEL}. Drop {LORE_DB_PATH} or change LORE_EMBEDDING_MODEL."
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?), (?, ?)",
        ("embedding_model", EMBEDDING_MODEL, "embedding_dim", str(EMBEDDING_DIM))
    )
    conn.commit()
```

### `lore/embed.py`

```python
"""Embedding wrapper. Lazy-loads the model on first call."""
from functools import lru_cache
import numpy as np
from sentence_transformers import SentenceTransformer

from .config import EMBEDDING_MODEL, EMBEDDING_DIM


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    # CPU-only; on arrstack this is fine for our corpus size.
    m = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
    actual = m.get_sentence_embedding_dimension()
    if actual != EMBEDDING_DIM:
        raise RuntimeError(
            f"Model {EMBEDDING_MODEL} produces dim {actual}, config says {EMBEDDING_DIM}"
        )
    return m


def embed(texts: list[str], *, batch_size: int = 32) -> np.ndarray:
    """Returns float32 array, shape (len(texts), EMBEDDING_DIM).

    Embeddings are L2-normalized — bge models are trained with normalization,
    and sqlite-vec's default distance assumes unit vectors for cosine.
    """
    if not texts:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
    vecs = _model().encode(
        texts, batch_size=batch_size, show_progress_bar=False,
        normalize_embeddings=True,
    )
    return vecs.astype(np.float32)
```

### `lore/fetchers/_base.py`

```python
"""Common interface every source-specific fetcher implements."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class RawDocument:
    source: str            # 'lia', 'wikipedia', ...
    source_id: str         # stable id within source (URL slug, article title)
    title: str
    url: str
    published: str | None  # ISO date if known, else None
    raw_text: str          # cleaned plain text, no HTML


class Fetcher(ABC):
    """Source-specific scraper. Each source = one subclass.

    The discover/fetch split exists so we can list source_ids cheaply
    (for incremental sync planning) before committing to body downloads.
    """
    name: str  # short identifier used as documents.source

    @abstractmethod
    def discover(self) -> list[str]:
        """Return source_ids to fetch. Cheap — no document body downloads."""
        ...

    @abstractmethod
    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        """Yield RawDocument for each id (or all from discover() if None)."""
        ...
```

### `lore/normalize.py`

```python
"""RawDocument -> list[Chunk]. Dumb-but-reasonable chunking for v1.

mentioned_songs stays empty in this phase; song matching against
dead.db.songs lands in a later phase along with the real fetchers.
"""
import re
from dataclasses import dataclass
from datetime import datetime

from .config import CHUNK_SIZE, CHUNK_OVERLAP
from .fetchers._base import RawDocument

DATE_RE = re.compile(r"\b(19[6-9]\d|20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")


@dataclass
class Chunk:
    chunk_index: int
    text: str
    mentioned_dates: list[str]
    mentioned_songs: list[str]
    era: str | None


def _approx_tokens(s: str) -> int:
    return len(s) // 4  # cheap proxy; good enough for sizing


def _era_for(dates: list[str]) -> str | None:
    """Coarse era label from the earliest in-band date in the chunk."""
    if not dates:
        return None
    try:
        d = min(datetime.fromisoformat(x) for x in dates)
    except ValueError:
        return None
    y = d.year
    if y < 1972: return "pigpen"
    if y < 1979: return "keith"
    if y < 1990: return "brent"
    if y <= 1995: return "bruce"
    return None


def normalize(doc: RawDocument) -> list[Chunk]:
    """Split on paragraph boundaries, then merge to ~CHUNK_SIZE tokens with overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", doc.raw_text) if p.strip()]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    idx = 0

    def flush():
        nonlocal buf, buf_tokens, idx
        if not buf:
            return
        text = "\n\n".join(buf)
        dates = sorted({m.group(0) for m in DATE_RE.finditer(text)})
        chunks.append(Chunk(
            chunk_index=idx, text=text,
            mentioned_dates=dates, mentioned_songs=[],
            era=_era_for(dates),
        ))
        idx += 1
        # overlap: keep tail paragraphs ~CHUNK_OVERLAP tokens
        tail, tail_tokens = [], 0
        for p in reversed(buf):
            tail.insert(0, p); tail_tokens += _approx_tokens(p)
            if tail_tokens >= CHUNK_OVERLAP:
                break
        buf = tail
        buf_tokens = tail_tokens

    for p in paras:
        pt = _approx_tokens(p)
        if buf_tokens + pt > CHUNK_SIZE and buf:
            flush()
        buf.append(p); buf_tokens += pt

    flush()
    return chunks
```

### `lore/build_lore_db.py`

```python
"""Orchestrator: fetchers -> normalize -> embed -> write. Idempotent."""
import json
from datetime import datetime, timezone

from .config import LORE_DB_PATH
from .db import connect, init_schema
from .embed import embed
from .normalize import normalize
from .fetchers._base import Fetcher


def ingest(fetcher: Fetcher, *, db_path: str = LORE_DB_PATH) -> tuple[int, int]:
    """Run one fetcher through the pipeline. Returns (docs_written, chunks_written).

    Idempotent: upserts documents on (source, source_id), wipes and re-creates
    chunks + vectors for each doc on every ingest. Safe to re-run after
    chunking-strategy changes or content updates.
    """
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    n_docs = n_chunks = 0
    for doc in fetcher.fetch():
        cur.execute("""
            INSERT INTO documents(source, source_id, title, url, published, fetched_at, raw_text)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                title=excluded.title, url=excluded.url, published=excluded.published,
                fetched_at=excluded.fetched_at, raw_text=excluded.raw_text
            RETURNING id
        """, (doc.source, doc.source_id, doc.title, doc.url, doc.published, now, doc.raw_text))
        doc_id = cur.fetchone()[0]

        # wipe old chunks + vectors for this doc (re-chunk on every ingest)
        old_ids = [r[0] for r in cur.execute(
            "SELECT id FROM chunks WHERE document_id=?", (doc_id,))]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            cur.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({ph})", old_ids)
            cur.execute(f"DELETE FROM chunks WHERE id IN ({ph})", old_ids)

        chunks = normalize(doc)
        if not chunks:
            continue
        vecs = embed([c.text for c in chunks])

        for c, v in zip(chunks, vecs):
            cur.execute("""
                INSERT INTO chunks(document_id, chunk_index, text,
                                    mentioned_dates, mentioned_songs, era)
                VALUES(?,?,?,?,?,?) RETURNING id
            """, (doc_id, c.chunk_index, c.text,
                  json.dumps(c.mentioned_dates), json.dumps(c.mentioned_songs), c.era))
            chunk_id = cur.fetchone()[0]
            cur.execute("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
                        (chunk_id, v.tobytes()))
            n_chunks += 1
        n_docs += 1

    conn.commit()
    conn.close()
    return n_docs, n_chunks
```

### `lore/query.py`

```python
"""Read-only semantic search over chunks."""
import json
from dataclasses import dataclass

from .config import LORE_DB_PATH
from .db import connect
from .embed import embed


@dataclass
class ChunkResult:
    chunk_id: int
    distance: float
    text: str
    source: str
    title: str
    url: str
    mentioned_dates: list[str]
    mentioned_songs: list[str]
    era: str | None


def search(query: str, *, k: int = 5, source: str | None = None,
           db_path: str = LORE_DB_PATH) -> list[ChunkResult]:
    """Top-k semantic matches for `query`. Filter by `source` if given."""
    conn = connect(db_path, readonly=True)
    qvec = embed([query])[0].tobytes()

    sql = """
        SELECT v.chunk_id, v.distance,
               c.text, c.mentioned_dates, c.mentioned_songs, c.era,
               d.source, d.title, d.url
        FROM chunk_vectors v
        JOIN chunks c    ON c.id = v.chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE v.embedding MATCH ? AND k = ?
    """
    params: list = [qvec, k * 4 if source else k]  # over-fetch when filtering
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    sql += " ORDER BY v.distance LIMIT ?"
    params.append(k)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        ChunkResult(
            chunk_id=r[0], distance=r[1], text=r[2],
            mentioned_dates=json.loads(r[3] or "[]"),
            mentioned_songs=json.loads(r[4] or "[]"),
            era=r[5], source=r[6], title=r[7], url=r[8],
        ) for r in rows
    ]
```

### `lore/smoke_test.py`

```python
"""End-to-end smoke: hardcoded fetcher, ingest, query, assert retrieval works.

Run: python3 -m lore.smoke_test
Exits 0 on success, nonzero on failure. Uses a temp DB so it doesn't touch
the real /hddpool/datastore/dead_lore.db.
"""
import os
import sys
import tempfile
from typing import Iterator

# point at temp DB BEFORE importing anything that reads config
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["LORE_DB_PATH"] = _tmp.name

from lore.fetchers._base import Fetcher, RawDocument  # noqa: E402
from lore.build_lore_db import ingest  # noqa: E402
from lore.query import search  # noqa: E402


class FakeFetcher(Fetcher):
    name = "smoketest"

    def discover(self) -> list[str]:
        return ["cornell77", "veneta72", "studio_workingmans"]

    def fetch(self, source_ids=None) -> Iterator[RawDocument]:
        yield RawDocument(
            source=self.name, source_id="cornell77",
            title="Cornell 5/8/77",
            url="https://example.invalid/cornell77",
            published="1977-05-08",
            raw_text=(
                "The Cornell 1977-05-08 show at Barton Hall is widely regarded as "
                "one of the greatest Grateful Dead performances ever recorded. "
                "The Scarlet Begonias into Fire on the Mountain segue from this "
                "show became the definitive version. Betty Cantor-Jackson's "
                "soundboard recording circulated for decades before official "
                "release on May 2017.\n\n"
                "Morning Dew from this show is often cited as the emotional peak "
                "of the spring 1977 tour."
            ),
        )
        yield RawDocument(
            source=self.name, source_id="veneta72",
            title="Veneta 8/27/72",
            url="https://example.invalid/veneta72",
            published="1972-08-27",
            raw_text=(
                "The 1972-08-27 Veneta Oregon show at the Springfield Creamery "
                "benefit is legendary for its second-set Dark Star and the "
                "ferocious afternoon heat. The Sunshine Daydream film captures "
                "the day."
            ),
        )
        yield RawDocument(
            source=self.name, source_id="studio_workingmans",
            title="Workingman's Dead studio notes",
            url="https://example.invalid/workingmans",
            published="1970-06-14",
            raw_text=(
                "Workingman's Dead, released in June 1970, marked a sharp turn "
                "toward acoustic Americana. Studio sessions were quick and the "
                "songwriting reflected the band's growing collaboration with "
                "Robert Hunter."
            ),
        )


def main() -> int:
    n_docs, n_chunks = ingest(FakeFetcher())
    assert n_docs == 3, f"expected 3 docs, got {n_docs}"
    assert n_chunks >= 3, f"expected >=3 chunks, got {n_chunks}"

    # the famous-show query should retrieve Cornell, not the studio album
    results = search("what's the greatest Dead show ever", k=3)
    assert results, "no results returned"
    top = results[0]
    assert top.source == "smoketest"
    assert "cornell" in top.title.lower() or "1977-05-08" in top.mentioned_dates, (
        f"top result was {top.title!r}, dates={top.mentioned_dates}; expected Cornell"
    )

    # era extraction should work
    cornell_chunks = [r for r in results if "1977-05-08" in r.mentioned_dates]
    assert cornell_chunks and cornell_chunks[0].era == "keith", (
        f"expected era=keith for 1977-05-08, got "
        f"{cornell_chunks[0].era if cornell_chunks else None}"
    )

    print(f"OK: {n_docs} docs, {n_chunks} chunks, top result = {top.title!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### `requirements.txt` (add these three lines, don't remove anything)

```
sentence-transformers>=3.0
sqlite-vec>=0.1.6
numpy>=1.24
```

When installing torch as a transitive dependency, use the CPU-only wheel index if pip pulls in CUDA wheels by default on arrstack. The relevant pip flag is `--index-url https://download.pytorch.org/whl/cpu` for torch, but only if the default install pulls CUDA. Check first; don't pre-emptively add complexity.

---

## Success criteria

The implementation is done when **all** of these are true:

1. All files exist at the paths above. `lore/__init__.py` and `lore/fetchers/__init__.py` exist as empty files.
2. `requirements.txt` has the three new lines appended, and nothing existing was removed or modified.
3. `pip install -r requirements.txt` on arrstack completes without errors. (One-time ~800 MB download for torch is expected.)
4. `cd /home/bear/dead-db && python3 -m lore.smoke_test` exits with code 0 and prints a line starting with `OK: 3 docs, ...`.
5. No changes to any other file in the repo (`build_db.py`, `plex.py`, `scrape_archive.py`, `build_archive.py`, `README.md`, etc.).
6. No git commit. The user reviews the diff and commits manually.

Don't claim success on a failing smoke test. If it fails, debug until it passes — don't modify the test's assertions to make it pass.

---

## Out of scope (explicitly — do not build these in this phase)

- **Real fetchers.** Light Into Ashes scraper, Wikipedia API client, Deadcast transcript ingest — separate phases, one per source.
- **MCP tool registration.** No new tools in `homelab-mcp/tools/deaddb.py`. The `query.search()` function is what those tools will call later; for now it's just a library.
- **Router (`dead_ask`).** The "run SQL + RAG in parallel, LLM merges" tool is the last phase, not this one.
- **Song matching.** `chunks.mentioned_songs` is populated as an empty list `[]` in this phase. Real matching against `dead.db.songs.name` is a later phase.
- **Docker, systemd, deployment automation.** This is a library + a smoke test. No services, no containers, no service files.
- **GPU acceleration via Intel iGPU / OpenVINO.** Decided against. The iGPU stays with Immich (and is reserved for future Whisper work on Deadcast).
- **Larger embedding model** (bge-base, bge-large, nomic, etc.). Decided against for this corpus size. If retrieval quality is poor, that's a future swap, not a now decision.
- **`unresolved_titles.log`-style logging** of chunks that fail entity extraction. Not needed in this phase — entity extraction is best-effort, empty results are fine.

---

## Notes for the implementer

- **Read this whole file before touching code.** The design decisions section exists because each item has a reason; the spec is dense for a reason.
- **If something is ambiguous, ask before improvising.** Small "improvements" compound into drift. Better to ask one question than rebuild after review.
- **The smoke test is the contract.** It exercises every layer (sqlite-vec wiring, model load, chunking, vector search, retrieval correctness). If you modify it, you're modifying the contract — flag it.
- **Don't add scope.** No CLI wrappers, no logging frameworks, no type stubs files, no Dockerfile, no Makefile, no `if __name__ == "__main__"` blocks beyond what's in the spec. If the spec doesn't ask for it, don't add it.
- **Style:** match the existing dead-db codebase (terse docstrings, no f-string overuse in SQL, minimal abstraction). Look at `build_db.py` and `plex.py` for tone.
