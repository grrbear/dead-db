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
USER_AGENT = "dead-db-lore/0.1 (personal homelab project; https://quickswoodcapital.com)"
ARTICLES_FILE = Path(__file__).parent.parent / "articles.txt"
STUB_MIN_BYTES = 2000          # extract shorter than this = stub, skip
REQUEST_PAUSE_S = 0.2          # polite gap between API calls
TITLES_PER_CALL = 1            # TextExtracts returns extract for only one page per call


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
