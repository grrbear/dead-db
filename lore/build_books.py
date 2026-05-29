"""Build the books corpus into dead_lore.db. Run: python3 -m lore.build_books"""
from .build_lore_db import ingest
from .fetchers.books import BooksFetcher


def main() -> int:
    n_docs, n_chunks = ingest(BooksFetcher())
    print(f"[books] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 25, f"expected >=25 books after dedup, got {n_docs}"
    assert n_chunks >= n_docs * 5, (
        f"expected >=5 chunks per book on average, got {n_chunks} for {n_docs} docs"
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
