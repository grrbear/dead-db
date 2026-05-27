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
