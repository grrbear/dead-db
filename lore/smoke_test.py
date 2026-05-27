"""End-to-end smoke: hardcoded fetcher, ingest, query, assert retrieval works.

Run: python3 -m lore.smoke_test
Exits 0 on success, nonzero on failure. Uses a temp DB so it doesn't touch
the real /hddpool/datastore/dead_lore.db.
"""
import os
import sys
import tempfile
from typing import Iterator

# point at temp DB BEFORE importing anything that reads config
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["LORE_DB_PATH"] = _tmp.name

from lore.fetchers._base import Fetcher, RawDocument  # noqa: E402
from lore.build_lore_db import ingest  # noqa: E402
from lore.query import search  # noqa: E402


class FakeFetcher(Fetcher):
    name = "smoketest"

    def discover(self) -> list[str]:
        return ["cornell77", "veneta72", "studio_workingmans"]

    def fetch(self, source_ids=None) -> Iterator[RawDocument]:
        yield RawDocument(
            source=self.name, source_id="cornell77",
            title="Cornell 5/8/77",
            url="https://example.invalid/cornell77",
            published="1977-05-08",
            raw_text=(
                "The Cornell 1977-05-08 show at Barton Hall is widely regarded as "
                "one of the greatest Grateful Dead performances ever recorded. "
                "The Scarlet Begonias into Fire on the Mountain segue from this "
                "show became the definitive version. Betty Cantor-Jackson's "
                "soundboard recording circulated for decades before official "
                "release on May 2017.\n\n"
                "Morning Dew from this show is often cited as the emotional peak "
                "of the spring 1977 tour."
            ),
        )
        yield RawDocument(
            source=self.name, source_id="veneta72",
            title="Veneta 8/27/72",
            url="https://example.invalid/veneta72",
            published="1972-08-27",
            raw_text=(
                "The 1972-08-27 Veneta Oregon show at the Springfield Creamery "
                "benefit is legendary for its second-set Dark Star and the "
                "ferocious afternoon heat. The Sunshine Daydream film captures "
                "the day."
            ),
        )
        yield RawDocument(
            source=self.name, source_id="studio_workingmans",
            title="Workingman's Dead studio notes",
            url="https://example.invalid/workingmans",
            published="1970-06-14",
            raw_text=(
                "Workingman's Dead, released in June 1970, marked a sharp turn "
                "toward acoustic Americana. Studio sessions were quick and the "
                "songwriting reflected the band's growing collaboration with "
                "Robert Hunter."
            ),
        )


def main() -> int:
    n_docs, n_chunks = ingest(FakeFetcher())
    assert n_docs == 3, f"expected 3 docs, got {n_docs}"
    assert n_chunks >= 3, f"expected >=3 chunks, got {n_chunks}"

    # the famous-show query should retrieve Cornell, not the studio album
    results = search("what's the greatest Dead show ever", k=3)
    assert results, "no results returned"
    top = results[0]
    assert top.source == "smoketest"
    assert "cornell" in top.title.lower() or "1977-05-08" in top.mentioned_dates, (
        f"top result was {top.title!r}, dates={top.mentioned_dates}; expected Cornell"
    )

    # era extraction should work
    cornell_chunks = [r for r in results if "1977-05-08" in r.mentioned_dates]
    assert cornell_chunks and cornell_chunks[0].era == "keith", (
        f"expected era=keith for 1977-05-08, got "
        f"{cornell_chunks[0].era if cornell_chunks else None}"
    )

    print(f"OK: {n_docs} docs, {n_chunks} chunks, top result = {top.title!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
