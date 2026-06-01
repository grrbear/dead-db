"""HeadyVersion scraper: song index -> song pages -> submissions.

Used by BOTH:
  - build_headyversion.py (structured: writes community_votes in dead.db)
  - the lore Fetcher interface (writes top-N blurbs to dead_lore.db)

stdlib only. Respects robots.txt: never fetches /s2s/comments/.
"""
import html
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

from ._base import Fetcher, RawDocument, Section
from ._html import html_to_text

BASE = "https://headyversion.com"
INDEX_URL = f"{BASE}/search/all/?order=count"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
              "dead-db-lore/0.1 (+https://quickswoodcapital.com)")
REQUEST_PAUSE_S = 1.0
TOP_BLURBS_PER_SONG = 5

SONG_LINK_RE = re.compile(
    r"""href=['"]?(/song/(\d+)/grateful-dead/([^/]+)/)['"]?"""
)
# A submission block (heuristic, validated by presence of score div):
# we extract via separate regexes pinned to the score's submission_id.
SCORE_RE = re.compile(
    r'<div\s+class="score"\s+id="show_score_(\d+)">\s*(-?\d+)\s*</div>',
    re.IGNORECASE,
)
SHOW_ID_RE = re.compile(r'data-show-id="(\d+)"')
# Submission canonical URL: /submission/<id>/<slug>/
SUBMISSION_LINK_RE = re.compile(
    r'href="(/submission/(\d+)/[^"]+)"'
)
# Show-page link inside a submission: /show/<id>/grateful-dead/<date>/
SHOW_PAGE_LINK_RE = re.compile(
    r'href="/show/(\d+)/grateful-dead/(\d{4}-\d{2}-\d{2})/"'
)


def _http_get(url: str, *, accept_redirects: bool = True) -> tuple[int, str, str]:
    """Return (status, final_url, body). 1.0s pause is the caller's job."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, url, ""


# ---------- discovery ----------

COUNT_RE = re.compile(r'(\d+)\s+versions?', re.IGNORECASE)


@dataclass(frozen=True)
class SongLink:
    heady_song_id: int
    slug: str
    url: str          # absolute
    expected_count: int = 0


def discover_songs() -> list[SongLink]:
    """Single fetch of /search/all/?order=count. Returns ~375 song URLs with expected counts."""
    _, _, body = _http_get(INDEX_URL)
    seen: set[int] = set()
    out: list[SongLink] = []
    matches = list(SONG_LINK_RE.finditer(body))
    for i, m in enumerate(matches):
        rel, sid_s, slug = m.group(1), m.group(2), m.group(3)
        sid = int(sid_s)
        if sid in seen:
            continue
        seen.add(sid)
        window_end = matches[i + 1].start() if i + 1 < len(matches) else min(m.end() + 400, len(body))
        window = body[m.end():window_end]
        cm = COUNT_RE.search(window)
        expected = int(cm.group(1)) if cm else 0
        out.append(SongLink(heady_song_id=sid, slug=slug, url=urljoin(BASE, rel), expected_count=expected))
    return out


# ---------- song-page parsing ----------

@dataclass
class HVSubmission:
    submission_id: int
    heady_song_id: int
    song_name: str            # the HV-displayed song name (page title-derived)
    song_url: str             # the /song/<id>/.../  URL we fetched it from
    show_id: int | None
    show_date: str | None     # ISO YYYY-MM-DD
    venue: str | None         # often blank on song page; populated later from show page
    city: str | None          # ditto
    vote_score: int
    blurb: str
    submission_url: str       # /submission/<id>/<slug>/


SONG_TITLE_RE = re.compile(
    r"Grateful Dead best ([^<|]+?)\s*\|\s*headyversion",
    re.IGNORECASE,
)


def _parse_song_name(body: str) -> str | None:
    """Extract the HV-rendered song name from the <title> tag."""
    m = re.search(r"<title>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    t = SONG_TITLE_RE.search(m.group(1))
    return html.unescape(t.group(1).strip()) if t else None


def _slice_submissions(body: str) -> list[tuple[int, int, str]]:
    """Return [(submission_id, vote_score, html_window)] from one song page.

    html_window is the chunk of HTML between this score div and the next
    one (or end of page). We parse the rest of the submission's fields
    from that window. This is the most stable way to associate fields
    with submissions without committing to a full HTML parse.
    """
    matches = list(SCORE_RE.finditer(body))
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((int(m.group(1)), int(m.group(2)), body[start:end]))
    return out


def _blurb_from_window(window: str) -> str:
    """Strip HTML and extract the blurb text from a submission's HTML window.

    Trim common cruft (vote score number, "Make a comment" links, etc.)
    The window contains the score, the show link, and the nominator's blurb.
    """
    text, _ = html_to_text(window)
    # remove leading score digits if html_to_text kept them
    text = re.sub(r"^\s*-?\d+\s*", "", text)
    # remove standardized control phrases
    for noise in (
        "Make a comment", "Add vote", "Listen on archive",
        "Listen on Archive", "comment", "comments",
    ):
        text = text.replace(noise, " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_song_page(body: str, song_url: str) -> tuple[str | None, list[HVSubmission]]:
    """Parse one song page's HTML. Returns (song_name, submissions)."""
    song_name = _parse_song_name(body)
    subs: list[HVSubmission] = []

    # heady_song_id from the URL itself (stable)
    m = re.search(r"/song/(\d+)/", song_url)
    heady_song_id = int(m.group(1)) if m else -1

    for sub_id, score, window in _slice_submissions(body):
        show_match = SHOW_PAGE_LINK_RE.search(window)
        show_id = int(show_match.group(1)) if show_match else None
        show_date = show_match.group(2) if show_match else None

        sub_match = SUBMISSION_LINK_RE.search(window)
        sub_url = urljoin(BASE, sub_match.group(1)) if sub_match else \
                  f"{BASE}/submission/{sub_id}/"

        blurb = _blurb_from_window(window)

        subs.append(HVSubmission(
            submission_id=sub_id,
            heady_song_id=heady_song_id,
            song_name=song_name or "",
            song_url=song_url,
            show_id=show_id,
            show_date=show_date,
            venue=None, city=None,
            vote_score=score,
            blurb=blurb,
            submission_url=sub_url,
        ))
    return song_name, subs


# ---------- pagination + show metadata ----------

BACKOFF_S = [1, 5, 15, 45, 60]


def _log_failed_page(url: str, status: int) -> None:
    log = Path(__file__).parent.parent / "headyversion_failed_pages.log"
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{status}\t{url}\n")


def _fetch_with_retry(url: str) -> tuple[int, str]:
    """Fetch url with exponential backoff on 5xx / network errors."""
    last_status = 0
    for attempt in range(len(BACKOFF_S) + 1):
        status, _, body = _http_get(url)
        time.sleep(REQUEST_PAUSE_S)
        if 0 < status < 500:
            return status, body
        last_status = status
        if attempt < len(BACKOFF_S):
            wait = BACKOFF_S[attempt]
            print(f"[hv] HTTP {status} on {url!r}, retry {attempt + 1}/{len(BACKOFF_S)} in {wait}s")
            time.sleep(wait)
    print(f"[hv] gave up on {url!r} after {len(BACKOFF_S) + 1} attempts (last status {last_status})")
    _log_failed_page(url, last_status)
    return last_status, ""


def fetch_song_submissions(song_url: str) -> list[HVSubmission]:
    """Walk ?page=1, ?page=2, ... until a page yields no new submissions."""
    seen: set[int] = set()
    out: list[HVSubmission] = []
    page = 1
    while True:
        url = f"{song_url}?page={page}" if page > 1 else song_url
        status, body = _fetch_with_retry(url)
        if status >= 400 or not body:
            break
        _, subs = parse_song_page(body, song_url)
        new = [s for s in subs if s.submission_id not in seen]
        if not new:
            break
        for s in new:
            seen.add(s.submission_id)
            out.append(s)
        page += 1
        if page > 100:  # hard safety stop; real terminator is "no new IDs" above
            break
    return out


VENUE_RE = re.compile(
    r"<title>([^|]+?)\s*\|\s*headyversion", re.IGNORECASE
)


# unused as of addendum; show metadata now sourced via JOIN in MCP tools
def fetch_show_metadata(show_id: int) -> tuple[str | None, str | None, str | None]:
    """Return (venue, city, archive_id) for a HV show_id. Best effort.

    archive_id comes from the redirect at /show/<id>/archive/.
    """
    venue = city = archive_id = None
    # show page for venue / city
    status, _, body = _http_get(f"{BASE}/show/{show_id}/")
    time.sleep(REQUEST_PAUSE_S)
    if status == 200 and body:
        # title is typically "<Date> - <Venue> <City> | headyversion"
        m = VENUE_RE.search(body)
        if m:
            # we don't try to split venue from city precisely; store the
            # raw title segment as venue and leave city NULL if absent.
            title = m.group(1).strip()
            # strip a leading "<Mon. D, YYYY> - " if present
            title = re.sub(r"^[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}\s*-\s*", "", title)
            venue = title or None

    # archive.org id via the /archive/ redirect
    status, final_url, _ = _http_get(f"{BASE}/show/{show_id}/archive/")
    time.sleep(REQUEST_PAUSE_S)
    m = re.search(r"archive\.org/details/([^/?#]+)", final_url or "")
    if m:
        archive_id = m.group(1)

    return venue, city, archive_id


# ---------- top-level iterator ----------

def iter_submissions(
    *,
    fetch_show_meta: bool = False,
    songs: list[SongLink] | None = None,
) -> Iterator[HVSubmission]:
    """Yield every submission across every song.

    songs: pre-discovered list (skips the index fetch); defaults to discover_songs().
    fetch_show_meta: legacy ON-path; addendum says always pass False — venue/city
                     come from JOIN to shows at query time.
    """
    if songs is None:
        songs = discover_songs()
        print(f"[hv] discovered {len(songs)} songs from index")

    show_cache: dict[int, tuple[str | None, str | None, str | None]] = {}

    for i, sl in enumerate(songs):
        subs = fetch_song_submissions(sl.url)
        print(f"[hv] {i+1}/{len(songs)} song_id={sl.heady_song_id} "
              f"slug={sl.slug} -> {len(subs)} submissions")
        for s in subs:
            if fetch_show_meta and s.show_id and s.show_id not in show_cache:
                show_cache[s.show_id] = fetch_show_metadata(s.show_id)
            if s.show_id and s.show_id in show_cache:
                v, c, aid = show_cache[s.show_id]
                s.venue, s.city = v, c
                s.archive_id = aid   # type: ignore[attr-defined]
            else:
                s.archive_id = None  # type: ignore[attr-defined]
            yield s


# ---------- Fetcher implementation for the lore path ----------

class HeadyVersionFetcher(Fetcher):
    """Lore-path entry: emits top-N blurbs per song as RawDocuments.

    Each blurb becomes one short document (typically 1-3 sentences).
    The normalizer paragraph-merges these into one or a few tiny chunks.
    """
    name = "headyversion"

    def discover(self) -> list[str]:
        return [s.url for s in discover_songs()]

    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        # group by song, keep top-N by score, emit as RawDocuments
        import os
        import sqlite3
        from collections import defaultdict

        # one-shot venue/city lookup from dead.db; avoids HTTP for show metadata
        dead_db = os.environ.get("DEAD_DB_PATH", "/hddpool/datastore/dead.db")
        show_meta: dict[str, tuple[str | None, str | None]] = {}
        with sqlite3.connect(f"file:{dead_db}?mode=ro", uri=True) as cx:
            for d, v, c in cx.execute("SELECT date, venue, city FROM shows"):
                show_meta[d] = (v, c)

        by_song: dict[int, list[HVSubmission]] = defaultdict(list)
        for s in iter_submissions(fetch_show_meta=False):
            by_song[s.heady_song_id].append(s)

        for heady_song_id, subs in by_song.items():
            subs.sort(key=lambda s: s.vote_score, reverse=True)
            top = subs[:TOP_BLURBS_PER_SONG]
            for rank, s in enumerate(top, start=1):
                if not s.blurb or not s.song_name:
                    continue
                v, c = show_meta.get(s.show_date or "", (None, None))
                title = f"{s.song_name} — {s.show_date or 'unknown date'}"
                text = (
                    f"[HeadyVersion top {rank} for {s.song_name}, "
                    f"{s.show_date or 'date unknown'}"
                    + (f" at {v}" if v else "")
                    + f", vote score {s.vote_score}]\n\n"
                    f"{s.blurb}"
                )
                yield RawDocument(
                    source="headyversion",
                    source_id=str(s.submission_id),
                    title=title,
                    url=s.submission_url,
                    published=s.show_date,
                    raw_text=text,
                    sections=[Section(heading=f"#{rank}", text=text)],
                    metadata={
                        "song": s.song_name,
                        "show_date": s.show_date,
                        "venue": v,
                        "city": c,
                        "vote_score": s.vote_score,
                        "rank_within_song": rank,
                        "heady_song_id": heady_song_id,
                    },
                )
