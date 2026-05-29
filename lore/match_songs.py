"""Post-pass: walk every chunk in dead_lore.db, run match_songs(), overwrite
chunks.mentioned_songs. Idempotent. Run: python3 -m lore.match_songs
"""
import json
import sqlite3
from collections import Counter

from .config import LORE_DB_PATH
from .db import connect
from .song_matcher import match_songs, to_json_list


def main() -> int:
    conn = connect(LORE_DB_PATH)
    cur = conn.cursor()
    n_chunks = cur.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"[match_songs] scanning {n_chunks} chunks")

    stats: Counter = Counter()
    name_counts: Counter = Counter()
    BATCH = 500
    offset = 0
    while True:
        rows = cur.execute(
            "SELECT id, text FROM chunks ORDER BY id LIMIT ? OFFSET ?",
            (BATCH, offset)
        ).fetchall()
        if not rows:
            break
        updates = []
        for chunk_id, text in rows:
            matches = match_songs(text)
            updates.append((json.dumps(to_json_list(matches)), chunk_id))
            stats["chunks_scanned"] += 1
            if matches:
                stats["chunks_with_match"] += 1
                stats["total_matches"] += len(matches)
                for m in matches:
                    name_counts[m.name] += 1
        cur.executemany(
            "UPDATE chunks SET mentioned_songs = ? WHERE id = ?",
            updates
        )
        offset += BATCH
        conn.commit()

    conn.close()

    print(f"[match_songs] scanned: {stats['chunks_scanned']}")
    print(f"[match_songs] chunks with >=1 match: {stats['chunks_with_match']}")
    print(f"[match_songs] total matches: {stats['total_matches']}")
    print(f"[match_songs] top 25 songs by chunk count:")
    for name, n in name_counts.most_common(25):
        print(f"  {n:5d}  {name}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
