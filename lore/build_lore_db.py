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
        metadata_json = json.dumps(doc.metadata) if doc.metadata is not None else None
        cur.execute("""
            INSERT INTO documents(source, source_id, title, url, published, fetched_at, raw_text, metadata)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                title=excluded.title, url=excluded.url, published=excluded.published,
                fetched_at=excluded.fetched_at, raw_text=excluded.raw_text,
                metadata=excluded.metadata
            RETURNING id
        """, (doc.source, doc.source_id, doc.title, doc.url, doc.published, now, doc.raw_text, metadata_json))
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
                                    mentioned_dates, mentioned_songs, era, section)
                VALUES(?,?,?,?,?,?,?) RETURNING id
            """, (doc_id, c.chunk_index, c.text,
                  json.dumps(c.mentioned_dates), json.dumps(c.mentioned_songs), c.era, c.section))
            chunk_id = cur.fetchone()[0]
            cur.execute("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
                        (chunk_id, v.tobytes()))
            n_chunks += 1
        n_docs += 1

    conn.commit()
    conn.close()
    return n_docs, n_chunks
