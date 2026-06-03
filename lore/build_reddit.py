"""Build the r/gratefuldead corpus into dead_lore.db.
Run: python3 -m lore.build_reddit"""
from .build_lore_db import ingest
from .fetchers.reddit import RedditFetcher


def main() -> int:
    n_docs, n_chunks = ingest(RedditFetcher())
    print(f"[reddit] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 30, f"expected >=30 quality docs, got {n_docs}"
    assert n_chunks >= n_docs, f"expected >=1 chunk/doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
