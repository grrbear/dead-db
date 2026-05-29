"""Build the LIA corpus into dead_lore.db. Run: python3 -m lore.build_lia"""
from .build_lore_db import ingest
from .fetchers.lia import LIAFetcher


def main() -> int:
    n_docs, n_chunks = ingest(LIAFetcher())
    print(f"[lia] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 100, f"expected >=100 docs across both sites, got {n_docs}"
    assert n_chunks >= n_docs, f"expected >=1 chunk per doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
