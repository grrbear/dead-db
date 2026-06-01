"""Build top-N blurbs per song into dead_lore.db.

Run: python3 -m lore.build_headyversion_lore

Note: this re-runs the HV scrape. If you've ALREADY run
build_headyversion (top-level), you'd be re-fetching the same data —
that's acceptable at this scale (~25 min) and keeps the two paths
fully independent. A future optimization could read from
community_votes instead of re-scraping; out of scope for now.
"""
from .build_lore_db import ingest
from .fetchers.headyversion import HeadyVersionFetcher


def main() -> int:
    n_docs, n_chunks = ingest(HeadyVersionFetcher())
    print(f"[hv-lore] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 1000, f"expected >=1000 top-N blurb docs, got {n_docs}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
