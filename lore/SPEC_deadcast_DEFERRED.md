---
# DEFERRED — do not implement as written

This spec is preserved for reference but is NOT the path forward for
the Deadcast corpus. dead.net is behind a Cloudflare-class WAF that
filters on TLS fingerprint, not just User-Agent. Both stdlib urllib
(any headers) and the Wayback Machine (whose crawler hit the same
filter) return a ~800-3000 char nav shell instead of transcript
content. The transcripts are server-rendered but unreachable from a
stdlib HTTP client.

The future path for this corpus is Whisper transcription of the
ART19-hosted episode audio on the Intel iGPU, paired with the
existing dead.net index page for episode metadata (title, season,
air date — which IS reachable). That becomes its own focused
project after phase 3 ships, not a sub-phase of it.

The fetcher pattern in this spec (index parsing, source value
distinction, _html.py reuse, marker-based content validation) is
still useful as a template — only the fetch step changes.
---

# dead-db phase 3 — Deadcast fetcher (corpus #3)

## Context

Third real corpus for the phase 3 lore RAG: transcripts of the official
Grateful Dead podcast, "The Good Ol' Grateful Deadcast." Builds on the
scaffolding and reuses the shared HTML cleaner (`lore/fetchers/_html.py`)
and the `sections` / `chunks.section` machinery from earlier phases.

Read `lore/SPEC.md`, `lore/SPEC_wikipedia.md`, and `lore/SPEC_lia.md` before
this file. This spec assumes all three are implemented and committed.

**Key finding (resolves a long-open question): NO WHISPER NEEDED.** The
transcripts already exist as text on dead.net, linked from a single index
page. This is a clean HTML fetch, same pattern as LIA — not an audio
transcription project. The iGPU stays free.

Source: https://www.dead.net/deadcast-index — a single page listing all
episodes across 13 seasons, each with a podcast-page link, an air date, and
a `[transcript]` link in brackets WHEN a transcript exists.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db; raw_text
in documents; code in dead-db/lore/; section hints via RawDocument.sections;
stdlib only (urllib, xml/html parsers — no requests/bs4/lxml). See prior specs.

## New decisions for this phase

- **The index page is the discovery mechanism.** Fetch
  https://www.dead.net/deadcast-index once per run, parse the episode lines.
  Each line yields: episode title, podcast URL, air date, season number, and
  the transcript URL if a `[transcript]` link is present. Re-runnable: the
  author backfills transcripts over time, so re-running picks up newly added
  ones. Index URL is hardcoded (no manifest file).
- **Transcripts where available; blurb fallback otherwise.** Coverage is
  uneven (Season 1 yes, Seasons 2-3 currently no, Season 4+ mostly yes, newest
  episodes not yet). For an episode WITH a transcript link: fetch the
  transcript page, that's the document. For an episode WITHOUT one: fetch the
  podcast page and use its episode description/blurb as a thin fallback doc.
- **Two source values to mark transcript vs stub.** Full transcripts ->
  `source="deadcast"`. Blurb-only fallback docs -> `source="deadcast_blurb"`.
  No new schema column. This lets the future router treat thin blurb stubs
  differently from rich transcripts (down-weight or filter). Same pattern as
  the two LIA sites.
- **Season is captured as a lead line, not a new column.** Prepend
  `[Deadcast Season N: <episode title>]` to the document text so the season
  and episode context get embedded. (Mirrors how LIA labels are prepended.)
- **`published` is the episode AIR date, not the show date discussed.** Same
  trap as Plex year / blog post dates. Many episode TITLES contain the show
  date the episode is about (e.g. "Veneta, 8/27/72", "MSG, 3/81"); the
  in-text/in-title date regex in normalize.py handles those. The air date
  from the index goes in RawDocument.published.

---

## Index page structure (reference for implementer)

The index renders (in markdown-ish extracted form) as season headings
followed by one line per episode. Two shapes:

WITH transcript:
```
[Enter Keith Godchaux](https://www.dead.net/deadcast/enter-keith-godchaux)  (9/16/21) [[transcript](https://www.dead.net/enter-keith-godchaux)]
```
WITHOUT transcript:
```
[American Beauty 50: Box of Rain](https://www.dead.net/deadcast/american-beauty-50-box-rain)  (9/30/20)
```

Parsing target (per episode line):
- **episode title** — first markdown link text
- **podcast_url** — first markdown link href (contains `/deadcast/`)
- **air_date** — the `(M/D/YY)` in parens; normalize to ISO `YYYY-MM-DD`
- **transcript_url** — href of a second link whose text is "transcript", if
  present; else None
- **season** — from the most recent `### Season N` heading above the line

IMPORTANT: dead.net is a Drupal site and the page HTML is heavy with nav,
menus, mailing-list forms, and footer cruft (visible in the fetched content).
The episode list sits under the heading "Good Ol' Grateful Deadcast Index &
Transcripts". Parse only the season blocks; ignore everything else. A robust
approach: regex the episode lines directly rather than trying to walk the DOM
(the lines have a very regular `[title](deadcast-url) (date) [[transcript](url)]`
shape). Implementer may parse the raw HTML anchors instead if cleaner — your
call — but the link-pattern regex over extracted text is simplest and least
fragile against Drupal markup churn.

Some transcript hrefs have quirks (mixed case, stray characters, even a
non-ASCII apostrophe in a couple — e.g. `Garcia-'73`). Use the href EXACTLY
as given; do not normalize or "fix" it. If a fetch 404s, log it as a miss and
move on (the index occasionally has a typo'd transcript link).

---

## `lore/fetchers/deadcast.py` — the fetcher

```python
"""Deadcast fetcher: parses dead.net/deadcast-index, fetches transcript pages
(or episode-page blurbs as fallback). Reuses the shared HTML cleaner. stdlib only.
"""
import re
import time
import urllib.request
from typing import Iterator

from ._base import Fetcher, RawDocument
from ._html import html_to_text

INDEX_URL = "https://www.dead.net/deadcast-index"
USER_AGENT = "dead-db-lore/0.1 (personal homelab project; https://quickswoodcapital.com)"
REQUEST_PAUSE_S = 1.0
BLURB_MIN_CHARS = 200      # below this, even the blurb is useless; skip+log

# Episode line: [title](.../deadcast/slug) (M/D/YY) optionally [[transcript](url)]
SEASON_RE = re.compile(r"^#{1,6}\s*Season\s+(\d+)", re.MULTILINE)
EPISODE_RE = re.compile(
    r"\[(?P<title>[^\]]+)\]\((?P<purl>https://www\.dead\.net/deadcast/[^)]+)\)"
    r"\s*\((?P<date>\d{1,2}/\d{1,2}/\d{2})\)"
    r"(?:\s*\[\[transcript\]\((?P<turl>https://www\.dead\.net/[^)]+)\)\])?"
)


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _iso_date(mdy: str) -> str | None:
    try:
        m, d, y = mdy.split("/")
        yyyy = "20" + y if int(y) < 50 else "19" + y
        return f"{yyyy}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def _parse_index(text: str) -> list[dict]:
    """Return episode dicts with title, purl, date, turl, season.

    Walks the text, tracking the current season heading, collecting episode
    matches under it. `text` should be the fetched index page (raw HTML is
    fine — the link patterns survive; or pre-clean with html_to_text first).
    """
    episodes = []
    # split on season headings, keep the season number with each block
    parts = SEASON_RE.split(text)
    # parts = [preamble, "1", block1, "2", block2, ...]
    for i in range(1, len(parts), 2):
        season = int(parts[i])
        block = parts[i + 1] if i + 1 < len(parts) else ""
        for m in EPISODE_RE.finditer(block):
            episodes.append({
                "title": m.group("title").strip(),
                "purl": m.group("purl"),
                "date": _iso_date(m.group("date")),
                "turl": m.group("turl"),
                "season": season,
            })
    return episodes


class DeadcastFetcher(Fetcher):
    name = "deadcast"   # per-doc source set below (deadcast / deadcast_blurb)

    def discover(self) -> list[str]:
        """Return podcast URLs of all episodes found in the index."""
        idx = _http_get(INDEX_URL)
        return [e["purl"] for e in _parse_index(idx)]

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        idx = _http_get(INDEX_URL)
        episodes = _parse_index(idx)
        misses: list[str] = []

        for e in episodes:
            time.sleep(REQUEST_PAUSE_S)
            if e["turl"]:
                # full transcript path
                try:
                    page = _http_get(e["turl"])
                except Exception:
                    misses.append(f"{e['title']} (transcript 404: {e['turl']})")
                    # fall through to blurb fallback below
                    e["turl"] = None
            if e["turl"]:
                text, sections = html_to_text(page)
                source = "deadcast"
                url = e["turl"]
            else:
                # blurb fallback: fetch the podcast page, use its description
                try:
                    ppage = _http_get(e["purl"])
                except Exception:
                    misses.append(f"{e['title']} (episode page 404: {e['purl']})")
                    continue
                text, sections = html_to_text(ppage)
                source = "deadcast_blurb"
                url = e["purl"]

            if len(text.strip()) < BLURB_MIN_CHARS:
                misses.append(f"{e['title']} (too thin: {len(text.strip())} chars)")
                continue

            lead = f"[Deadcast Season {e['season']}: {e['title']}]"
            text = f"{lead}\n\n{text}"

            yield RawDocument(
                source=source,
                source_id=url,
                title=e["title"],
                url=url,
                published=e["date"],
                raw_text=text,
                sections=sections or None,
            )

        if misses:
            log = __import__("pathlib").Path(__file__).parent.parent / "deadcast_misses.log"
            log.write_text("\n".join(misses) + "\n", encoding="utf-8")
            print(f"[deadcast] {len(misses)} misses -> {log.name}")
```

Implementer notes:
- The regexes are the heart of this. Test `_parse_index` against the live
  index FIRST (print episode count + a few parsed dicts) before wiring up
  fetching. Expected: ~150 episode lines across 13 seasons, with the majority
  carrying a transcript URL.
- Decide once whether to feed `_parse_index` the raw HTML or the
  `html_to_text`-cleaned index. Raw HTML is more reliable for the link regex
  (cleaning may mangle the `[[transcript](url)]` bracket structure). Prefer
  raw HTML for INDEX parsing; use html_to_text only for the transcript/episode
  BODY pages. Note this is a deliberate split.
- The blurb-fallback path runs the full episode page through html_to_text,
  which will include nav/footer cruft from Drupal. The shared cleaner's
  comment/nav stripping helps but won't be perfect on a blurb. That's
  acceptable — blurb docs are explicitly marked `deadcast_blurb` and the
  BLURB_MIN_CHARS floor drops the truly useless ones. Do NOT over-engineer
  blurb extraction; these are stubs by design, replaced by real transcripts
  when the author backfills.
- Politeness: 1.0s between every fetch. ~150 episodes = a few minutes. Fine.

---

## `lore/build_deadcast.py` — runnable entry

```python
"""Build the Deadcast corpus into dead_lore.db. Run: python3 -m lore.build_deadcast"""
from .build_lore_db import ingest
from .fetchers.deadcast import DeadcastFetcher


def main() -> int:
    n_docs, n_chunks = ingest(DeadcastFetcher())
    print(f"[deadcast] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 80, f"expected >=80 episodes (transcripts + blurbs), got {n_docs}"
    assert n_chunks >= n_docs, f"expected >=1 chunk per doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

---

## Success criteria

1. `lore/fetchers/deadcast.py` and `lore/build_deadcast.py` exist. No changes
   to scaffolding contracts (reuse RawDocument.sections, chunks.section,
   _html.py — all already present).
2. Existing smoke test still passes unchanged
   (`python3 -m lore.smoke_test` -> "OK: 3 docs...").
3. `_parse_index` correctly parses the live index: prints ~150 episodes
   across 13 seasons; most have a transcript URL; Seasons 2 and 3 episodes
   correctly show transcript_url = None (blurb-fallback path).
4. `python3 -m lore.build_deadcast` runs, ingests >=80 docs total, prints
   doc/chunk counts, and writes deadcast_misses.log for any 404s / too-thin
   episodes.
5. Spot-check in a REPL (sanity, not asserts):
   ```python
   from lore.query import search
   search("what does the Deadcast say about Keith Godchaux joining", k=3)
   search("Sunshine Daydream Veneta 1972 film", k=3)
   ```
   Transcript chunks (source="deadcast") should surface and read as clean
   spoken-word prose with no nav/footer cruft. Confirm at least one result
   has source="deadcast" (not all blurbs).
6. Verify the two source values both exist:
   `SELECT source, COUNT(*) FROM documents GROUP BY source;` should show
   deadcast and (if any S2/S3/new episodes were reachable) deadcast_blurb.
7. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- Whisper / audio transcription — NOT NEEDED, transcripts are text. If
  tempted, stop: the whole point of checking the index was to avoid this.
- Song matching for mentioned_songs — still `[]` this phase (next phase).
- MCP tools (dead_lore, dead_ask) — later phase. Retrieval verified manually.
- Per-episode incremental sync — ingest() is idempotent at source_id (URL)
  grain; re-running re-fetches all, fine at this scale and picks up backfilled
  transcripts automatically.
- Scraping audio files, RSS enclosures, or Apple/Spotify podcast feeds — the
  dead.net index is the single source. Ignore the podcast platforms.
- bs4 / lxml / feedparser / requests — stdlib only.

---

## Notes for the implementer

- Read this file + the three prior specs before coding.
- Test `_parse_index` against the live index page BEFORE building the fetch
  loop. If the episode count is wildly off (e.g. <100 or >250), the regex is
  wrong — fix it before fetching anything. Show the user the parsed count and
  a sample if unsure.
- Use transcript hrefs EXACTLY as the index gives them (some have mixed case
  / odd characters). Don't normalize. 404s go to the miss log.
- INDEX parsing uses raw HTML (link regex); BODY parsing uses html_to_text.
  This split is deliberate — don't "unify" it.
- Be polite: 1.0s between fetches, sequential. dead.net is a commercial site
  but still — no hammering.
- If the live index structure has drifted from what this spec describes
  (headings renamed, link shape changed), STOP and tell the user rather than
  forcing the regex. The spec was written against the index as of 2026-05.
- Match existing dead-db style: terse docstrings, stdlib over deps.
