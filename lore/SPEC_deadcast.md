# dead-db phase 3 — Deadcast fetcher (corpus #3, local-HTML path)

> **Supersedes `SPEC_deadcast_DEFERRED.md`.** That spec was shelved because
> dead.net sits behind a WAF that blocks stdlib HTTP, so transcripts were
> unreachable from the box. That blocker is gone: the Season 1 transcript
> pages are now saved to the NAS by hand. This is a **local-file fetcher** —
> no network, no Wayback, no Whisper, no iGPU. The deferred spec's index
> parser and `deadcast_blurb` fallback are NOT used; ignore them.

## Context

Third real corpus for the phase 3 lore RAG: transcripts of "The Good Ol'
Grateful Deadcast." Builds on the scaffolding and reuses the shared HTML
cleaner (`lore/fetchers/_html.py`) and the chunking in `lore/normalize.py`.

Read `lore/SPEC.md`, `lore/SPEC_wikipedia.md`, and `lore/SPEC_lia.md` before
this file. Assumes all three are implemented and committed.

Input: complete-saved dead.net transcript pages on the NAS at
`/hddpool/datastore/mediacenter/Audio/Deadcast/`. Initial batch is Season 1
(the "Workingman's Dead 50" episode arc, 8 song episodes) plus 2 BONUS
episodes — 10 `*.html` files, each with a sibling `<name>_files/` asset
directory and macOS `._*` AppleDouble companions.

This spec was written against a real saved file
(`Workingman's Dead 50_ Dire Wolf _ Grateful Dead.html`); the DOM facts below
are verified, not assumed.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate `dead_lore.db`;
`raw_text` in `documents`; code in `dead-db/lore/`; section hints via
`RawDocument.sections`; stdlib only (urllib, `html.parser`, `re` — no
requests/bs4/lxml). Entity extraction (`mentioned_dates`, `mentioned_songs`,
`era`) happens downstream in `normalize.py` — **the fetcher does not touch
it**, same as every other fetcher.

## New decisions for this phase

- **Local files are the source. No network at all.** `discover()` globs the
  Deadcast directory; `fetch()` reads each file from disk. The WAF problem
  that deferred this corpus is moot.
- **One document per saved `*.html` file.** Skip `._*` AppleDouble files and
  the `_files/` asset directories (the `*.html` glob already excludes the
  dirs; the `._` filter handles the rest).
- **Isolate the transcript container before cleaning.** A saved dead.net page
  is a full Drupal document — nav, mailing-list forms, footer, and ~28
  related-episode teaser blocks. The transcript lives in the **single largest
  `field--name-body` div**. Verified: on the Dire Wolf page the transcript
  block is ~38.5k chars of clean text; the next-largest body blocks are
  ~9.8k/8.7k teasers. Selection MUST size candidate blocks *after* removing
  `<script>`/`<style>` content — one body wrapper leads with a large inline
  CSS/JS blob that otherwise wins on raw length.
- **Strip `<strong>`/`<b>` before calling `html_to_text`.** Speaker labels are
  marked `<p><strong>JESSE:</strong> …</p>`. `_html.py` treats `strong`/`b`
  as heading boundaries, so leaving them in would fragment the transcript into
  ~77 one-utterance "sections" and destroy chunking. Stripping the bold tags
  (keeping inner text) leaves `JESSE:` inline and yields flat paragraphs that
  chunk correctly at ~512 tokens. **This is load-bearing — do not skip it.**
- **Force flat chunking.** Pass `sections=None` on the RawDocument. The
  transcripts have no real `<h2>/<h3>` structure (verified: zero heading tags
  in the body); the only emphasis was the speaker bolding we strip. Flat
  ~512-token paragraph-merged chunks are correct here.
- **Single source value: `source="deadcast"`.** No `deadcast_blurb` — that was
  the network spec's fallback for episodes whose transcripts hadn't been
  posted. Here, every saved file IS a transcript. No new schema column.
- **Season/episode as a lead line.** The body's opening lines carry
  `Season N, Episode M`. Prepend `[Deadcast Season N, Episode M: <title>]` to
  the doc text so the context embeds (mirrors LIA's label prepending). BONUS
  episodes that lack the pattern fall back to `[Deadcast: <title>]`.
- **`published` is None.** The saved transcript page carries no air-date meta
  tag (the deferred spec got air dates from the index page, which we no longer
  fetch). Leave `published=None`. In-text show dates (the dates discussed) are
  still extracted downstream by `normalize.py`'s date regex. Air-date backfill
  from the index is explicitly out of scope (see below).
- **URL/source_id from the page's canonical link.** Use
  `<link rel="canonical">` (fallback `og:url`, then the `saved from url=(...)`
  comment). Verified present: `https://www.dead.net/workingmans-dead-50-dire-wolf`.
  This makes `ingest()` idempotent at the real URL grain, so re-runs after
  adding more saved episodes upsert cleanly.

---

## Saved-page DOM (verified reference for implementer)

- `<title>Workingman's Dead 50: Dire Wolf | Grateful Dead</title>` — strip the
  trailing ` | Grateful Dead`. (The only `<h1>` is the hidden site-logo
  "Grateful Dead"; do NOT use it for the title.)
- Transcript: largest `<div class="clearfix text-formatted field
  field--name-body field--type-text-with-summary field--label-hidden
  field__item">…</div>`. ~96 `<p>` tags, speaker labels in `<strong>`, leads
  with a `<script>`/`<style>` blob that `html_to_text` drops anyway.
- Body opening (after cleaning): `Good Ol' Grateful Deadcast / Season 1,
  Episode 3 / Workingman's Dead 50: Dire Wolf / JESSE: …`
- Zero-width chars (`\u200b`, `\ufeff`) appear between words; strip them.

---

## `lore/fetchers/deadcast.py` — the fetcher

```python
"""Deadcast fetcher (local HTML): reads saved dead.net transcript pages from
the NAS, isolates the transcript body, emits RawDocuments. Reuses the shared
HTML cleaner. stdlib only.

Supersedes the deferred network-fetch design: the WAF blocker is gone because
the pages are saved to disk. No network, no Whisper.
"""
import html as _html
import os
import re
from pathlib import Path
from typing import Iterator

from ._base import Fetcher, RawDocument
from ._html import html_to_text

DEADCAST_DIR = os.environ.get(
    "DEADCAST_DIR", "/hddpool/datastore/mediacenter/Audio/Deadcast"
)
MIN_TRANSCRIPT_CHARS = 2000   # largest body block below this -> not a transcript; log+skip

SEASON_EP_RE = re.compile(r"Season\s+(\d+),\s*Episode\s+(\d+)", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.IGNORECASE)
OG_URL_RE = re.compile(r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"', re.IGNORECASE)
SAVED_FROM_RE = re.compile(r'saved from url=\(\d+\)(\S+)')
BODY_DIV_RE = re.compile(r'<div[^>]*field--name-body[^>]*>', re.IGNORECASE)
SCRIPT_STYLE_RE = re.compile(r'(?is)<script.*?</script>|<style.*?</style>')
BOLD_RE = re.compile(r'(?is)</?(?:strong|b)\b[^>]*>')
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _inner_div(doc: str, start: int) -> str:
    """Depth-tracked extraction of one <div>...</div> inner HTML from `start`."""
    i = doc.find(">", start) + 1
    depth = 1
    for d in re.finditer(r"<(/?)div\b", doc[i:]):
        depth += -1 if d.group(1) else 1
        if depth == 0:
            return doc[i:i + d.start()]
    return doc[i:]


def _clean_len(h: str) -> int:
    """Text length AFTER dropping script/style — for picking the real body."""
    return len(re.sub(r"<[^>]+>", " ", SCRIPT_STYLE_RE.sub(" ", h)))


def _largest_body(doc: str) -> str | None:
    """Inner HTML of the largest field--name-body div (the transcript)."""
    best, best_len = None, 0
    for m in BODY_DIV_RE.finditer(doc):
        inner = _inner_div(doc, m.start())
        n = _clean_len(inner)
        if n > best_len:
            best, best_len = inner, n
    return best


def _title(doc: str) -> str:
    m = TITLE_RE.search(doc)
    if not m:
        return "Deadcast episode"
    t = _html.unescape(m.group(1)).strip()
    return re.sub(r"\s*\|\s*Grateful Dead\s*$", "", t).strip()


def _url(doc: str, fallback: str) -> str:
    for rx in (CANONICAL_RE, OG_URL_RE, SAVED_FROM_RE):
        m = rx.search(doc)
        if m:
            return m.group(1).strip()
    return fallback


class DeadcastFetcher(Fetcher):
    name = "deadcast"

    def __init__(self, src_dir: str = DEADCAST_DIR):
        self.dir = Path(src_dir)

    def discover(self) -> list[str]:
        """Top-level *.html only; skip AppleDouble (._*); _files/ dirs excluded by glob."""
        return sorted(
            str(p) for p in self.dir.glob("*.html")
            if not p.name.startswith("._")
        )

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        paths = [Path(p) for p in (source_ids or self.discover())]
        misses: list[str] = []

        for path in paths:
            doc = path.read_text(encoding="utf-8", errors="replace")
            body = _largest_body(doc)
            if body is None:
                misses.append(f"{path.name} (no field--name-body div)")
                continue

            # strip bold so speaker labels don't become section headings
            body = BOLD_RE.sub("", body)
            text, _sections = html_to_text(body)   # sections intentionally discarded
            text = text.translate(ZERO_WIDTH).strip()
            if len(text) < MIN_TRANSCRIPT_CHARS:
                misses.append(f"{path.name} (too thin: {len(text)} chars)")
                continue

            title = _title(doc)
            url = _url(doc, fallback=path.stem)

            se = SEASON_EP_RE.search(text)
            if se:
                lead = f"[Deadcast Season {int(se.group(1))}, Episode {int(se.group(2))}: {title}]"
            else:
                lead = f"[Deadcast: {title}]"   # BONUS / non-numbered episodes
            text = f"{lead}\n\n{text}"

            yield RawDocument(
                source="deadcast",
                source_id=url,
                title=title,
                url=url,
                published=None,        # air date not on saved page; see spec notes
                raw_text=text,
                sections=None,         # force flat chunking
            )

        if misses:
            log = Path(__file__).parent.parent / "deadcast_misses.log"
            log.write_text("\n".join(misses) + "\n", encoding="utf-8")
            print(f"[deadcast] {len(misses)} misses -> {log.name}")
```

Implementer notes:
- **Verify on ONE file first.** Before running the full build, load a single
  saved page and confirm: `_largest_body` returns the transcript block (not a
  CSS/teaser block); cleaned text starts with "Good Ol' Grateful Deadcast …
  Season N, Episode M …"; no `gsc-`/`var(--` CSS leakage; `JESSE:` present
  inline. Show me that output. (This mirrors the deferred spec's "test the
  parser before fetching" gate.)
- **Check a BONUS file too.** The 2 bonus episodes may not carry the
  `Season N, Episode M` line — confirm they fall back to `[Deadcast: <title>]`
  and still produce a >2000-char transcript. If a bonus page's body structure
  differs (e.g. transcript not in `field--name-body`), log it and tell me
  rather than forcing it.
- The bold-strip and the script/style-aware body selection are the two
  non-obvious correctness points. Don't "simplify" either.
- `html_to_text` returns `(text, sections)`; we discard sections on purpose.
  Don't wire them into the RawDocument.
- Match existing dead-db style: terse docstrings, stdlib over deps.

---

## `lore/build_deadcast.py` — runnable entry

```python
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
```

---

## Success criteria

1. `lore/fetchers/deadcast.py` and `lore/build_deadcast.py` exist. No changes
   to scaffolding contracts (`_html.py`, `_base.py`, `normalize.py`,
   `schema.sql` all untouched).
2. Existing smoke test still passes unchanged (`python3 -m lore.smoke_test`).
3. One-file verification (above) shown to me before the full run.
4. `python3 -m lore.build_deadcast` runs, ingests one doc per saved `.html`
   (10 in the initial batch; floor `n_docs >= n_files - 1`), prints
   doc/chunk counts, writes `deadcast_misses.log` only if something was
   skipped.
5. Spot-check in a REPL (sanity, not asserts):
   ```python
   from lore.query import search
   search("what does the Deadcast say about Dire Wolf and don't murder me", k=3)
   search("Workingman's Dead recording sessions 1970", k=3)
   ```
   `source="deadcast"` chunks should surface and read as clean spoken-word
   prose — speaker cues like `JESSE:` inline, no nav/footer/CSS cruft.
6. `SELECT source, COUNT(*) FROM documents GROUP BY source;` shows `deadcast`
   alongside the existing corpora.
7. Retrieval through the MCP works with no tool code change: `dead_lore`
   /`dead_ask` with no `source` filter already surface the new chunks
   (the filter is pass-through). The `dead_lore` docstring's `source` enum
   now lists `'deadcast'` for discoverability.
8. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- Any network fetch of dead.net, the Wayback Machine, RSS, or podcast feeds.
  The local files are the only source.
- Whisper / audio transcription. Not needed — these are text.
- Air-date backfill from the dead.net index page. Could be a later nicety;
  `published=None` is fine for now.
- Song matching for `mentioned_songs` beyond what `normalize.py` already does.
- New MCP tools. Retrieval rides the existing `dead_lore`/`dead_ask`.
- Speaker diarization / per-utterance structuring. Flat chunks with inline
  `JESSE:` cues are enough.
- bs4 / lxml / requests. stdlib only.

---

## Notes for the implementer

- Read this file + `lore/SPEC.md`, `SPEC_wikipedia.md`, `SPEC_lia.md` first.
- This corpus grows by the user dropping more saved `*.html` into
  `DEADCAST_DIR` and re-running `build_deadcast`. `ingest()` is idempotent at
  `source_id` (the canonical URL), so re-runs upsert; no dedup work needed.
- If the live/saved DOM differs from the verified structure above (no
  `field--name-body`, transcript elsewhere, title format changed), STOP and
  show me — don't force the regex. Spec written against pages saved 2026-06.
