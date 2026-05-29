# dead-db phase 3 — Light Into Ashes fetcher (corpus #2)

## Context

Second real corpus for the phase 3 lore RAG. Builds on the scaffolding
(`lore/` package, schema, `Fetcher` ABC) and reuses the section-hint channel
(`RawDocument.sections`) and `chunks.section` column added by the Wikipedia
phase.

Read `lore/SPEC.md` (scaffolding) and `lore/SPEC_wikipedia.md` (corpus #1)
before this file. This spec assumes both are implemented and committed.

"Light Into Ashes" (LIA) is the pen name of the author of two sibling
Blogspot sites covering the early Grateful Dead:
- **deadessays.blogspot.com** — "Grateful Dead Guide." Long-form analytical
  essays (the gold: interpretation, synthesis, argument).
- **deadsources.blogspot.com** — "Grateful Dead Sources." Primary-source
  transcriptions (vintage press clippings, reviews, interviews — "what was
  printed at the time, without hindsight").

Both are active (deadessays has a Jan 2026 post). The fetcher must be
re-runnable to pick up new posts.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db; raw_text
stored in documents; code in dead-db/lore/; API/feed extracts not raw-markup
parsing where avoidable; section hints via RawDocument.sections; stdlib only
(urllib, no requests). See prior specs.

## New decisions for this phase

- **Both sites, distinguished by `source` value.** deadessays ->
  `source="lia_essays"`, deadsources -> `source="lia_sources"`. No new schema
  column. The future router can filter/label by source value to distinguish
  analysis from primary-source material. Do NOT add a doc_type column.
- **Feed is the spine, always.** Discovery (every post's URL, title, date,
  labels) comes from the Blogger Atom feed. No HTML parsing for discovery.
- **Feed content is the body when present.** Blogger's posts feed returns
  full post HTML in the Atom `<content>` element when the blog is configured
  for full feeds (the common default). Use it directly — no per-post fetch.
- **HTML fetch is a narrow per-post fallback, not a second scraper.** If a
  given post's feed `<content>` looks truncated, fetch THAT post's HTML page
  and extract the body. Most posts won't trigger this. Do NOT build a full
  standalone HTML scraper "as a fallback" — it's a targeted top-up only.
- **Comments are excluded.** Pull only the posts feed
  (`/feeds/posts/default`), never the comments feed. On the HTML fallback
  path, the extractor MUST strip the comment thread (these posts have 20-77
  comments each — the single biggest quality risk).
- **`published` is the POST date, not the date of events discussed.** Same
  trap as Plex `year`. In-text show dates are handled by normalize.py's date
  regex; the Atom `<published>`/`<updated>` goes in RawDocument.published.

---

## Blogger Atom feed mechanics (reference for implementer)

Feed URL pattern (per site):
```
https://deadessays.blogspot.com/feeds/posts/default?start-index=1&max-results=500&alt=atom
https://deadsources.blogspot.com/feeds/posts/default?start-index=1&max-results=500&alt=atom
```

- `max-results` caps at 500. Walk pagination with
  `start-index = 1, 501, 1001, ...` until an empty page (no `<entry>`).
- Each `<entry>` contains:
  - `<title>` — post title
  - `<published>` and `<updated>` — ISO timestamps
  - `<link rel="alternate" type="text/html" href="...">` — the post URL
    (this is the stable source_id)
  - `<content type="html">` — the post body as escaped HTML (when full feed)
  - `<category term="...">` — Blogger labels (e.g. "1966", "1972"). Capture
    these; they're useful metadata (often the year/era).
- Atom namespace is `http://www.w3.org/2005/Atom`. Parse with stdlib
  `xml.etree.ElementTree`. No external feed library.

Truncation detection for the fallback: treat feed content as truncated if,
after HTML->text cleaning, it ends with an ellipsis marker ("…", "...",
"[...]", "Read more") OR is shorter than ~500 chars. When truncated, fetch
the post URL's HTML and extract the body instead.

---

## Shared HTML -> clean text + sections

Both the feed `<content>` (escaped HTML) and the fallback post pages are
HTML, so there is ONE cleaning step. Put it in a new module
`lore/fetchers/_html.py` so the LIA fetcher (and future Blogspot siblings)
share it.

```python
"""Shared HTML -> (clean_text, sections) for Blogspot-style post bodies.
stdlib only: html.parser. No bs4, no lxml, no requests.
"""
from html.parser import HTMLParser
from ._base import Section

# Tags whose entire subtree is dropped (scripts, styles, and — critically —
# Blogger comment containers on the HTML fallback path).
_DROP_SUBTREE = {"script", "style", "noscript"}
# Block tags that force a paragraph break in output text.
_BLOCK = {"p", "div", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
# Tags treated as section-heading boundaries for the sections hint.
_HEADING = {"h1", "h2", "h3", "h4", "b", "strong"}
# id/class substrings marking comment regions to drop on the HTML fallback.
_COMMENT_MARKERS = ("comment", "disqus", "comments")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._drop_depth = 0
        self._in_comment_block = 0
        self._parts: list[str] = []
        # heading capture
        self._cap_heading = False
        self._heading_buf: list[str] = []
        # (heading, text) accumulation for sections
        self._cur_heading = ""
        self._cur_text: list[str] = []
        self.sections: list[Section] = []

    def _flush_section(self):
        text = _collapse("".join(self._cur_text))
        if text:
            self.sections.append(Section(heading=self._cur_heading, text=text))
        self._cur_text = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        ident = " ".join(filter(None, [ad.get("id", ""), ad.get("class", "")])).lower()
        if tag in _DROP_SUBTREE or any(m in ident for m in _COMMENT_MARKERS):
            self._drop_depth += 1
            self._in_comment_block += 1 if any(m in ident for m in _COMMENT_MARKERS) else 0
            return
        if self._drop_depth:
            return
        if tag in _HEADING:
            # a new heading boundary: close current section, start capturing
            self._flush_section()
            self._cap_heading = True
            self._heading_buf = []
        elif tag in _BLOCK:
            self._parts.append("\n")
            self._cur_text.append("\n")

    def handle_endtag(self, tag):
        ad_drop = tag in _DROP_SUBTREE
        if self._drop_depth and (ad_drop or self._in_comment_block):
            self._drop_depth = max(0, self._drop_depth - 1)
            if self._in_comment_block:
                self._in_comment_block = max(0, self._in_comment_block - 1)
            return
        if self._drop_depth:
            return
        if tag in _HEADING and self._cap_heading:
            self._cur_heading = _collapse("".join(self._heading_buf))
            self._cap_heading = False

    def handle_data(self, data):
        if self._drop_depth:
            return
        if self._cap_heading:
            self._heading_buf.append(data)
        self._parts.append(data)
        self._cur_text.append(data)

    def close(self):
        super().close()
        self._flush_section()


def _collapse(s: str) -> str:
    # collapse runs of whitespace, preserve paragraph breaks
    lines = [ln.strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln); blank = False
        elif not blank:
            out.append(""); blank = True
    return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, list[Section]]:
    """Returns (clean_text, sections). sections may be empty if no headings."""
    ex = _Extractor()
    ex.feed(html)
    ex.close()
    text = _collapse("".join(ex._parts))
    # de-dupe: if only one section and it equals the whole text, treat as flat
    sections = ex.sections if len(ex.sections) > 1 else []
    return text, sections
```

Implementer notes:
- This is best-effort cleaning, not a perfect HTML renderer. Good enough for
  embedding. Do not pull in bs4/lxml to "do it properly" — stdlib only.
- The comment-stripping (`_COMMENT_MARKERS`) only matters on the HTML
  fallback path; feed `<content>` has no comments. Keep it anyway — harmless
  on feed content, essential on fallback.
- If `sections` comes back empty (no headings found), the document is flat
  and normalize.py uses its paragraph-merge fallback. That's fine.

---

## `lore/fetchers/lia.py` — the fetcher

```python
"""Light Into Ashes fetcher (deadessays + deadsources Blogspot sites).
Feed-first discovery + body; per-post HTML fallback only when feed content
is truncated. stdlib only.
"""
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Iterator

from ._base import Fetcher, RawDocument
from ._html import html_to_text

ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "dead-db-lore/0.1 (personal homelab project; https://quickswoodcapital.com)"
MAX_RESULTS = 500
REQUEST_PAUSE_S = 1.0          # polite: this is one person's blog
TRUNCATION_MIN_CHARS = 500
TRUNCATION_MARKERS = ("…", "...", "[...]", "read more")

SITES = {
    "lia_essays": "https://deadessays.blogspot.com",
    "lia_sources": "https://deadsources.blogspot.com",
}


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _looks_truncated(text: str) -> bool:
    if len(text) < TRUNCATION_MIN_CHARS:
        return True
    tail = text[-40:].lower()
    return any(m in tail for m in TRUNCATION_MARKERS)


class LIAFetcher(Fetcher):
    """One fetcher, both sites. Each site emits a distinct source value."""
    name = "lia"   # not used directly; per-doc source is set per-site

    def discover(self) -> list[str]:
        """Return all post URLs across both sites (the stable source_ids)."""
        urls = []
        for base in SITES.values():
            urls.extend(self._discover_site(base))
        return urls

    def _discover_site(self, base: str) -> list[str]:
        urls, start = [], 1
        while True:
            feed_url = (f"{base}/feeds/posts/default"
                        f"?start-index={start}&max-results={MAX_RESULTS}&alt=atom")
            root = ET.fromstring(_http_get(feed_url))
            entries = root.findall(f"{ATOM}entry")
            if not entries:
                break
            for e in entries:
                link = e.find(f"{ATOM}link[@rel='alternate']")
                if link is not None:
                    urls.append(link.get("href"))
            start += MAX_RESULTS
            time.sleep(REQUEST_PAUSE_S)
        return urls

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        for source, base in SITES.items():
            yield from self._fetch_site(source, base)

    def _fetch_site(self, source: str, base: str) -> Iterator[RawDocument]:
        start = 1
        while True:
            feed_url = (f"{base}/feeds/posts/default"
                        f"?start-index={start}&max-results={MAX_RESULTS}&alt=atom")
            root = ET.fromstring(_http_get(feed_url))
            entries = root.findall(f"{ATOM}entry")
            if not entries:
                break
            for e in entries:
                title_el = e.find(f"{ATOM}title")
                link_el = e.find(f"{ATOM}link[@rel='alternate']")
                content_el = e.find(f"{ATOM}content")
                pub_el = e.find(f"{ATOM}published")
                if link_el is None:
                    continue
                url = link_el.get("href")
                title = (title_el.text or "").strip() if title_el is not None else url
                published = (pub_el.text or "")[:10] if pub_el is not None else None
                labels = [c.get("term") for c in e.findall(f"{ATOM}category")
                          if c.get("term")]

                body_html = content_el.text if content_el is not None else ""
                text, sections = html_to_text(body_html or "")

                if _looks_truncated(text):
                    # narrow fallback: fetch the post page HTML, re-extract
                    try:
                        page = _http_get(url).decode("utf-8", "replace")
                        text, sections = html_to_text(page)
                    except Exception:
                        pass  # keep whatever feed gave us
                    time.sleep(REQUEST_PAUSE_S)

                if not text.strip():
                    continue

                # prepend Blogger labels as a lead line so they're embedded too
                if labels:
                    text = f"[labels: {', '.join(labels)}]\n\n{text}"

                yield RawDocument(
                    source=source,
                    source_id=url,
                    title=title,
                    url=url,
                    published=published,
                    raw_text=text,
                    sections=sections or None,
                )
            start += MAX_RESULTS
            time.sleep(REQUEST_PAUSE_S)
```

Implementer notes:
- `name = "lia"` is a placeholder; the real per-document `source` is set
  per-site inside the loop (`lia_essays` / `lia_sources`). This is a
  deliberate deviation from the one-fetcher-one-source pattern because the
  two sites share all logic and differ only by base URL + source label.
- Politeness: 1.0s pause between every HTTP call. This is one person's blog,
  not an API. Do not parallelize. Do not lower the pause.
- Discovery walks the feed; fetch ALSO walks the feed (re-fetches). At this
  corpus size (~hundreds of posts) that's fine and keeps the code simple.
  The discover() method exists to satisfy the ABC and for future incremental
  use; fetch() does not depend on it.

---

## `lore/build_lia.py` — runnable entry

```python
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
```

---

## Success criteria

1. `lore/fetchers/_html.py`, `lore/fetchers/lia.py`, `lore/build_lia.py`
   exist. No changes to scaffolding contracts (RawDocument and chunks.section
   already exist from the Wikipedia phase — reuse, don't re-add).
2. The existing smoke test still passes unchanged
   (`python3 -m lore.smoke_test` -> "OK: 3 docs...").
3. `python3 -m lore.build_lia` runs against both live sites, ingests >=100
   documents total, prints doc/chunk counts. (deadessays alone has ~hundreds
   of posts going back to 2009; both sites combined easily clears 100.)
4. Spot-check in a REPL — these are sanity checks, not asserts:
   ```python
   from lore.query import search
   search("why was Pigpen the most talented early member", k=3)
   search("the proto-Solomon jam 1972 1973 Dark Star", k=3)
   ```
   Essays from deadessays should surface. Verify retrieved text is clean
   prose with NO comment-thread cruft and NO navigation/sidebar text.
5. Manually verify comment stripping: pick one post known to have many
   comments, confirm its chunks contain none of the comment text.
6. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- Sibling blogs (hooterollin, lostlivedead, jgmf) — the SITES dict makes them
  a one-line add later; not now.
- The comments feed — never ingested.
- Song matching for mentioned_songs — still `[]` this phase.
- MCP tools (dead_lore, dead_ask) — later phase. Retrieval verified manually.
- Incremental sync (only-fetch-new-posts) — ingest() is idempotent at
  source_id (URL) grain; re-running re-fetches everything, fine at this scale.
- bs4 / lxml / feedparser / requests — stdlib only. If tempted to add one,
  STOP and ask.

---

## Notes for the implementer

- Read this file + lore/SPEC.md + lore/SPEC_wikipedia.md before coding.
- The single biggest quality risk is comment-thread and sidebar/nav text
  leaking into chunks on the HTML fallback path. The _html.py comment
  stripping addresses it; verify with criterion 5 above before declaring done.
- Be polite: 1.0s between HTTP calls, sequential only. This is a person's
  passion project blog, not infrastructure.
- If feed content turns out to be truncated for MANY posts (not just a few),
  that means LIA runs summary-only feeds — STOP and tell the user before
  hammering the site with hundreds of fallback page fetches. That's a
  design conversation, not an improvisation.
- Match existing dead-db style: terse docstrings, stdlib over deps.
