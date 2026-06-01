"""Build the Deadcast corpus into dead_lore.db. Run: python3 -m lore.build_deadcast"""
from .build_lore_db import ingest
from .fetchers.deadcast import DeadcastFetcher


def main() -> int:
    fetcher = DeadcastFetcher()
    n_files = len(fetcher.discover())
    n_docs, n_chunks = ingest(fetcher)
    print(f"[deadcast] {n_files} files -> {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= n_files - 1, f"expected ~1 doc per file ({n_files}), got {n_docs}"
    assert n_chunks >= n_docs, f"expected >=1 chunk per doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
