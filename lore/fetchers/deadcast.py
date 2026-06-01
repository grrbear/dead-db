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
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)


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
