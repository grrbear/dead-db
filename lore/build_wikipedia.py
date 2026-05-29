"""Build the Wikipedia corpus into dead_lore.db. Run: python3 -m lore.build_wikipedia"""
from .build_lore_db import ingest
from .fetchers.wikipedia import WikipediaFetcher


def main() -> int:
    n_docs, n_chunks = ingest(WikipediaFetcher())
    print(f"[wikipedia] ingested {n_docs} docs, {n_chunks} chunks")
    # validation floor — fail loud if the corpus came back suspiciously thin
    assert n_docs >= 100, f"expected >=100 docs, got {n_docs} (check articles.txt / network)"
    assert n_chunks >= n_docs, f"expected >=1 chunk per doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
