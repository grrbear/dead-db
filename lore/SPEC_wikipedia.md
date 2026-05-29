# dead-db phase 3 — Wikipedia fetcher (corpus #1)

## Context

First real corpus for the phase 3 lore RAG. The scaffolding (`lore/` package,
`documents`/`chunks`/`chunk_vectors` schema, `Fetcher` ABC, smoke test) is
already built and committed — this spec adds the first source that produces
real `RawDocument`s.

Wikipedia is deliberately first (before Light Into Ashes): the API returns
clean text, so we validate the fetcher pattern, the section-aware chunking,
and real retrieval quality without fighting Blogspot HTML.

Read `lore/SPEC.md` (the scaffolding spec) before this file. This spec
assumes everything in it.

---

## Locked decisions carried from scaffolding (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db;
raw_text stored in documents; code in dead-db/lore/. See lore/SPEC.md.

## New decisions for this phase

- **Curated, not crawled.** Articles come from a hand-maintained file
  `lore/articles.txt`, one Wikipedia title per line. Code reads it; never
  owns it. The file is the curation surface and is expected to grow.
- **API extracts, never wikitext.** Use the MediaWiki API `extracts` prop
  with `explaintext=1`. We never parse raw wikitext / templates / infoboxes.
  This is the extensible choice: the fetcher's contract is "clean text +
  optional structural hints," which transfers to LIA (HTML) and Deadcast
  (transcript) unchanged. Markup parsing does not transfer and is banned.
- **Section structure is captured as a hint, not parsed from markup.**
  The `extracts` endpoint can return section-delimited plain text. We use
  section headings as preferred chunk boundaries and as per-chunk metadata.
- **Two deliberate, flagged changes to scaffolding contracts** (details
  below): an optional `sections` field on `RawDocument`, and a `section`
  column on `chunks`. Both are backward compatible — the existing smoke
  test must still pass unchanged.

---

## Contract change 1: `RawDocument.sections` (optional)

Add an optional field to the dataclass in `lore/fetchers/_base.py`. This is
the channel every structured source uses to declare its natural break points.

```python
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Section:
    heading: str          # e.g. "Early life", "Legacy"; "" for lead/intro
    text: str             # clean plain text of this section only


@dataclass
class RawDocument:
    source: str
    source_id: str
    title: str
    url: str
    published: str | None
    raw_text: str                          # full clean text (all sections joined)
    sections: list[Section] | None = None  # NEW — structural hint, None if flat
```

Rules:
- `raw_text` stays required and is the full document text. A fetcher with
  no structure (or the smoke test's FakeFetcher) leaves `sections=None`.
- When `sections` is present, `raw_text` MUST equal the section texts joined
  by `"\n\n"` in order (so re-chunking from either path is consistent).
- The existing smoke test passes `sections=None` implicitly (it doesn't set
  the field). It must continue to pass with zero changes.

## Contract change 2: `chunks.section` column

Add one column to the `chunks` table in `lore/schema.sql`:

```sql
    section TEXT,    -- source section heading this chunk came from, NULL if flat
```

Place it after `era`. It records which document section a chunk originated
from — useful retrieval metadata ("from the 'Legacy' section of Jerry
Garcia"). Update the `Chunk` dataclass in `normalize.py` and the INSERT in
`build_lore_db.py` to carry it.

This is additive. A fresh dead_lore.db gets the column; the smoke test's
flat documents store `section=NULL`. Because init_schema uses
`CREATE TABLE IF NOT EXISTS`, note that an EXISTING dead_lore.db will NOT
gain the column automatically — but we have no production lore DB yet (only
the throwaway smoke-test temp DB), so a fresh build is fine. Do NOT write a
migration; just ensure schema.sql has the column for new builds.

---

## `lore/normalize.py` — section-aware chunking

Update `normalize()` to prefer section boundaries when `doc.sections` is
present, falling back to the existing paragraph-merge logic when it's None.

Behavior:
- **If `doc.sections` is None:** current behavior, unchanged. `section=None`
  on every chunk.
- **If `doc.sections` is present:** chunk each section independently, never
  merging text across a section boundary. Within a section, apply the same
  ~CHUNK_SIZE token paragraph-merge with overlap. Tag every chunk produced
  from a section with that section's heading in `Chunk.section`.
  `chunk_index` remains globally sequential across the whole document.
- A section shorter than CHUNK_SIZE becomes a single chunk (don't pad/merge
  with the next section).
- Date/era extraction works the same way (regex over chunk text). The
  `mentioned_songs` field stays `[]` this phase, as in scaffolding.

Keep the `Chunk` dataclass change minimal:

```python
@dataclass
class Chunk:
    chunk_index: int
    text: str
    mentioned_dates: list[str]
    mentioned_songs: list[str]
    era: str | None
    section: str | None        # NEW
```

---

## `lore/fetchers/wikipedia.py` — the fetcher

```python
"""Wikipedia fetcher. Reads a curated title list, pulls clean section text
via the MediaWiki API. No wikitext parsing — extracts endpoint only.
"""
import time
import urllib.parse
import urllib.request
import json
from pathlib import Path
from typing import Iterator

from ._base import Fetcher, RawDocument, Section

API = "https://en.wikipedia.org/w/api.php"
# Wikipedia API etiquette: descriptive UA with contact. EDIT contact below.
USER_AGENT = "dead-db-lore/0.1 (personal homelab project; contact: bear@quickswoodcapital.com)"
ARTICLES_FILE = Path(__file__).parent.parent / "articles.txt"
STUB_MIN_BYTES = 2000          # extract shorter than this = stub, skip
REQUEST_PAUSE_S = 0.2          # polite gap between API calls
TITLES_PER_CALL = 20           # API allows up to 50; 20 is safe with extracts


def _read_titles(path: Path = ARTICLES_FILE) -> list[str]:
    """One title per line. '#' starts a comment. Blank lines ignored."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _api_get(params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _split_sections(extract: str) -> list[Section]:
    """Split an explaintext extract into sections on heading lines.

    The extracts endpoint renders headings as lines like:
        == Heading ==
        === Subheading ===
    Lead text before the first heading gets heading "".
    """
    sections: list[Section] = []
    cur_heading = ""
    cur_lines: list[str] = []

    def flush():
        text = "\n".join(cur_lines).strip()
        if text:
            sections.append(Section(heading=cur_heading, text=text))

    for line in extract.splitlines():
        stripped = line.strip()
        if stripped.startswith("==") and stripped.endswith("=="):
            flush()
            cur_heading = stripped.strip("=").strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    flush()
    return sections


class WikipediaFetcher(Fetcher):
    name = "wikipedia"

    def discover(self) -> list[str]:
        return _read_titles()

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        titles = source_ids or self.discover()
        unresolved: list[str] = []

        for i in range(0, len(titles), TITLES_PER_CALL):
            batch = titles[i:i + TITLES_PER_CALL]
            data = _api_get({
                "action": "query",
                "prop": "extracts|info",
                "explaintext": "1",
                "exsectionformat": "wiki",   # headings as == X == so we can split
                "inprop": "url",
                "redirects": "1",
                "titles": "|".join(batch),
            })
            pages = data.get("query", {}).get("pages", [])
            for page in pages:
                title = page.get("title", "")
                if page.get("missing"):
                    unresolved.append(title)
                    continue
                if "disambiguation" in (page.get("pageprops") or {}):
                    unresolved.append(f"{title} (disambiguation)")
                    continue
                extract = page.get("extract") or ""
                if len(extract.encode("utf-8")) < STUB_MIN_BYTES:
                    unresolved.append(f"{title} (stub)")
                    continue
                sections = _split_sections(extract)
                raw_text = "\n\n".join(s.text for s in sections) if sections else extract
                yield RawDocument(
                    source=self.name,
                    source_id=title,            # resolved (post-redirect) title
                    title=title,
                    url=page.get("fullurl", f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}"),
                    published=None,             # articles have no single date
                    raw_text=raw_text,
                    sections=sections or None,
                )
            time.sleep(REQUEST_PAUSE_S)

        if unresolved:
            log = ARTICLES_FILE.parent / "wikipedia_unresolved.log"
            log.write_text("\n".join(unresolved) + "\n", encoding="utf-8")
            print(f"[wikipedia] {len(unresolved)} unresolved titles -> {log.name}")
```

Notes for the implementer:
- **Edit the `USER_AGENT` contact** if bear@quickswoodcapital.com isn't the
  right address. Wikipedia wants a real contact per their API policy.
- `exsectionformat=wiki` makes the extract use `== Heading ==` markers we can
  split on. This is the one place we read a markup-ish marker, but it's the
  extracts endpoint's own rendering, not raw wikitext — no template parsing.
- Redirects are followed (`redirects=1`); `source_id` is the resolved title,
  so re-runs are idempotent and "Jerome Garcia" + "Jerry Garcia" don't
  double-ingest.
- Misses (missing pages, disambiguation, stubs) go to
  `lore/wikipedia_unresolved.log`. Reviewing this and fixing articles.txt is
  part of the workflow, not an afterthought.

---

## `lore/articles.txt` — seeded curated list

Create this file with the content below. It's maximal scope: albums, members,
family tree, side projects, venues, songs, cultural/scene. The user curates
it over time; this is the starting seed.

```
# Grateful Dead — curated Wikipedia article list for lore RAG.
# One article title per line. '#' starts a comment. Blank lines ignored.
# Titles must match Wikipedia exactly (incl. parenthetical disambiguators).
# Misses are logged to wikipedia_unresolved.log — fix titles here and re-run.

# === The band ===
Grateful Dead

# === Studio albums ===
The Grateful Dead (album)
Anthem of the Sun
Aoxomoxoa
Workingman's Dead
American Beauty
Wake of the Flood
From the Mars Hotel
Blues for Allah
Terrapin Station (album)
Shakedown Street (album)
Go to Heaven (album)
In the Dark (Grateful Dead album)
Built to Last (album)

# === Live albums (selected canon) ===
Live/Dead
Grateful Dead (1971 album)
Europe '72
Steal Your Face
Reckoning (Grateful Dead album)
Dead Set
Without a Net
Dick's Picks
Dave's Picks
Sunshine Daydream (album)
Cornell 5/8/77

# === Band members ===
Jerry Garcia
Bob Weir
Phil Lesh
Bill Kreutzmann
Mickey Hart
Ron "Pigpen" McKernan
Keith Godchaux
Donna Jean Godchaux
Brent Mydland
Vince Welnick
Bruce Hornsby
Tom Constanten
Robert Hunter (lyricist)
John Perry Barlow

# === Family tree / side projects ===
Jerry Garcia Band
New Riders of the Purple Sage
Old & In the Way
Legion of Mary (band)
Reconstruction (band)
Kingfish (band)
Bobby and the Midnites
Ratdog
The Other Ones
The Dead (band)
Furthur (band)
Dead & Company
Rhythm Devils
Planet Drum
Mickey Hart Band
Phil Lesh and Friends
The Mountain Girl
Merry Pranksters

# === Venues & live sound ===
Wall of Sound (Grateful Dead)
Fillmore West
Fillmore East
Winterland Ballroom
Barton Hall
The Warlocks (American band)

# === Songs (selected) ===
Truckin'
Casey Jones (Grateful Dead song)
Friend of the Devil
Ripple (song)
Box of Rain
Sugar Magnolia
Uncle John's Band
Dark Star (Grateful Dead song)
Terrapin Station (suite)
Touch of Grey
Scarlet Begonias
Fire on the Mountain (Grateful Dead song)
Eyes of the World
St. Stephen (song)
China Cat Sunflower
Morning Dew (song)
Playing in the Band
Estimated Prophet
Help on the Way

# === Scene / culture / legacy ===
Deadhead
Acid Tests
The Electric Kool-Aid Acid Test
Haight-Ashbury
Wall of Sound
Rex Foundation
Owsley Stanley
Bill Graham (promoter)
Jerry Garcia's guitars
Steal Your Face (logo)
Dancing bears (Grateful Dead)
Sunshine Daydream
Festival Express
The Grateful Dead Movie
Long Strange Trip (film)
```

Heads-up to the implementer: some of these titles will miss (Wikipedia is
finicky — `Wall of Sound (Grateful Dead)` vs the Spector article, `Touch of
Grey` spelling, songs that redirect to album articles). That's expected and
exactly what the unresolved log is for. Do NOT try to "fix" titles by
guessing alternatives in code — log the miss, leave articles.txt for the
user to correct.

---

## Wiring it together

No new orchestrator needed — `build_lore_db.ingest()` already takes any
Fetcher. Add a thin runnable entry so the user can build the Wikipedia corpus:

Create `lore/build_wikipedia.py`:

```python
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
```

---

## Success criteria

1. `RawDocument` has the optional `sections` field + `Section` dataclass;
   `lore/SPEC.md`'s existing smoke test (`python3 -m lore.smoke_test`) STILL
   PASSES UNCHANGED, printing `OK: 3 docs, 3 chunks, ...`.
2. `chunks` table has the `section` column; `normalize.py` and
   `build_lore_db.py` carry it through.
3. `lore/fetchers/wikipedia.py`, `lore/articles.txt`, and
   `lore/build_wikipedia.py` exist.
4. `python3 -m lore.build_wikipedia` runs against live Wikipedia, ingests
   >=100 documents, prints the doc/chunk counts, and writes
   `wikipedia_unresolved.log` listing any misses.
5. A manual retrieval check works, e.g. in a Python REPL:
   ```python
   from lore.query import search
   r = search("why did the band's sound change after Pigpen died", k=3)
   # at least one result; Pigpen / Keith-era article among top hits
   ```
   This is a sanity check, not an automated assert (live data varies).
6. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- LIA fetcher, Deadcast fetcher — later phases.
- Song matching for `mentioned_songs` — still `[]` this phase.
- MCP tools (`dead_lore`, `dead_ask`) — later phase. This phase is still
  library-only; retrieval is verified manually.
- Incremental / delta sync — `ingest()` is already idempotent at source_id
  grain; re-running re-fetches everything, which is fine at this scale.
- Migrations for an existing dead_lore.db — there is no production lore DB
  yet. Fresh build only.
- Any wikitext / template / infobox parsing. Extracts endpoint only.

---

## Notes for the implementer

- Read this whole file and lore/SPEC.md before touching code.
- The two contract changes (RawDocument.sections, chunks.section) are the
  only places you touch existing scaffolding. Touch nothing else there.
- If a contract change would break the existing smoke test, STOP — you've
  done it wrong; the changes are designed to be backward compatible.
- Edit the USER_AGENT contact address.
- If something is ambiguous, ask before improvising.
- Match existing dead-db style (terse docstrings, stdlib over deps —
  note this fetcher uses only urllib, no requests dependency).
