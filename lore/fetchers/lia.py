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
