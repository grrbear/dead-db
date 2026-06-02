"""Read-only semantic search over chunks."""
import json
from dataclasses import dataclass

from .config import LORE_DB_PATH
from .db import connect
from .embed import embed

# sqlite-vec applies `k` to the GLOBAL nearest neighbours before any joined
# source filter can run, so when filtering we over-fetch a generous pool from
# the index and narrow afterward. The corpus is only a few thousand chunks, so
# a fixed pool stays cheap and lets rare sources (e.g. deadcast) still surface.
_FILTER_POOL = 256


@dataclass
class ChunkResult:
    chunk_id: int
    distance: float
    text: str
    source: str
    title: str
    url: str
    section: str | None
    mentioned_dates: list[str]
    mentioned_songs: list[str]
    era: str | None


def search(query: str, *, k: int = 5, source: str | None = None,
           db_path: str = LORE_DB_PATH) -> list[ChunkResult]:
    """Top-k semantic matches for `query`. Filter by `source` if given.

    The KNN MATCH is isolated in a CTE so the vec0 query stays self-contained;
    the source filter is applied in the OUTER query. sqlite-vec does not
    reliably honour extra WHERE predicates (especially on joined tables) inside
    a `MATCH ... AND k = ?` query — doing so silently returns zero rows.
    """
    conn = connect(db_path, readonly=True)
    qvec = embed([query])[0].tobytes()

    pool = _FILTER_POOL if source else k

    sql = """
        WITH knn AS (
            SELECT chunk_id, distance
            FROM chunk_vectors
            WHERE embedding MATCH ? AND k = ?
        )
        SELECT knn.chunk_id, knn.distance,
               c.text, c.section, c.mentioned_dates, c.mentioned_songs, c.era,
               d.source, d.title, d.url
        FROM knn
        JOIN chunks c    ON c.id = knn.chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE (? IS NULL OR d.source = ?)
        ORDER BY knn.distance
        LIMIT ?
    """
    params: list = [qvec, pool, source, source, k]

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        ChunkResult(
            chunk_id=r[0], distance=r[1], text=r[2], section=r[3],
            mentioned_dates=json.loads(r[4] or "[]"),
            mentioned_songs=json.loads(r[5] or "[]"),
            era=r[6], source=r[7], title=r[8], url=r[9],
        ) for r in rows
    ]
