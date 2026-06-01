# dead-db — HeadyVersion ingest (community votes + blurbs)

## Context

HeadyVersion (headyversion.com) is the Deadhead community voting site:
fans nominate specific performances ("submissions") of each song, add a
short blurb explaining why, and other users upvote. Each submission is
keyed to (song, show_date) — exactly the shape of `dead.db.performances`,
which makes this the highest-value cross-corpus join in the project.

This spec is dual-path (Path C from the design conversation):

- **Structured path** — a new `community_votes` table in `dead.db`
  joining HeadyVersion submissions to your existing songs/shows. Populates
  two new MCP tools: `dead_top_versions` and `dead_show_votes`.
- **Lore path** — the top-5 highest-voted blurbs per song get ingested as
  chunks in `dead_lore.db` with rich metadata. `source="headyversion"`.
  Reuses 100% of the existing fetcher/normalize/embed pipeline.

Read `lore/SPEC.md`, `lore/SPEC_song_matching.md`, and
`lore/SPEC_mcp_tools.md` before this file. Assumes all phase 3 corpora,
song matching, and MCP tools are implemented and committed.

## Key recon findings (resolved, do not re-investigate)

- robots.txt is `User-agent: * / Disallow: /s2s/comments/`. We respect it.
  Comments are NOT ingested. Only blurbs + vote scores + show metadata.
- Site is jQuery-era Django, server-rendered HTML. stdlib urllib works
  with a normal browser-ish User-Agent. No Cloudflare, no JS rendering,
  no auth. Same fetch approach as Wikipedia/LIA — NOT like dead.net.
- Discovery: ONE fetch of `https://headyversion.com/search/all/?order=count`
  yields all ~375 song URLs in one HTML page (no JS).
- Song page structure: `/song/<song_id>/grateful-dead/<slug>/`, paginated
  via `?page=N` (Django endless-pagination; `?page=N` works without JS).
- Vote scores are integer-inline:
  `<div class="score" id="show_score_<submission_id>">439</div>`.
- Each submission carries `data-show-id="<show_id>"` on its row;
  archive.org identifier reachable via the redirect at
  `/show/<show_id>/archive/` → `archive.org/details/<identifier>`.
- ~27k submissions total across ~375 songs; most submissions have 0-5
  comments, a few popular ones have 90+. We're skipping comments, so
  ~1,200-1,500 song-page fetches at 1 req/sec ≈ 25 min build.

## Locked decisions

- **Path C** — structured votes table + top-N blurbs as lore chunks.
- **Respect robots.txt.** No comments. The structured table is the prize;
  blurbs are gravy.
- **Top 5 blurbs per song** (by vote score) get ingested as chunks. ALL
  submissions go into the structured table.
- **Structured table lives in `dead.db`**, not a sibling DB. Created
  idempotently by the HeadyVersion build script; `build_db.py`'s wipe
  pattern does NOT touch it.
- **Reuse existing infrastructure throughout:** `lore/fetchers/_html.py`
  for HTML→text, `lore/song_matcher.py`'s canonical-name lookup for
  resolving HeadyVersion song names to `dead.db.songs.uuid`,
  `build_lore_db.ingest` for the blurb-chunk path.
- **stdlib only.** urllib, html.parser (via _html.py), sqlite3, re.
  No requests, bs4, lxml.
- **No new schema column anywhere.** `community_votes` is its own table;
  blurb chunks use the existing `documents.metadata` JSON column.

---

## File layout

```
dead-db/
  build_headyversion.py            # NEW — top-level runnable, parallel to build_db.py
  lore/
    fetchers/
      headyversion.py              # NEW — HV scraper + parsers, used by both paths
  ...                              # all other lore/ files unchanged

homelab-mcp/
  tools/
    deaddb.py                      # MODIFIED — 2 new MCP tools added
```

The HeadyVersion fetcher lives under `lore/fetchers/` because the
lore-path uses it directly as a `Fetcher`, and the structured-path
reuses its parsers. Single source of truth for HV scraping.

---

## Schema: `community_votes` table in `dead.db`

```sql
CREATE TABLE IF NOT EXISTS community_votes (
    submission_id    INTEGER PRIMARY KEY,            -- HV's submission ID
    heady_song_id    INTEGER NOT NULL,               -- HV's internal song ID
    song_uuid        TEXT,                           -- FK songs.uuid; NULL if unresolved
    song_name        TEXT NOT NULL,                  -- HV's song name (as displayed)
    show_date        TEXT,                           -- YYYY-MM-DD; NULL if HV gave none
    venue            TEXT,
    city             TEXT,
    vote_score       INTEGER NOT NULL,
    blurb            TEXT,
    archive_id       TEXT,                           -- archive.org identifier
    heady_url        TEXT NOT NULL,                  -- canonical submission URL
    fetched_at       TEXT NOT NULL                   -- ISO datetime
);

CREATE INDEX IF NOT EXISTS idx_cv_song_uuid    ON community_votes(song_uuid);
CREATE INDEX IF NOT EXISTS idx_cv_show_date    ON community_votes(show_date);
CREATE INDEX IF NOT EXISTS idx_cv_vote_score   ON community_votes(vote_score DESC);
CREATE INDEX IF NOT EXISTS idx_cv_heady_song   ON community_votes(heady_song_id);
```

`song_uuid` and `show_date` are nullable — some HV submissions reference
shows not in your canonical DB, or song names that don't resolve. We
still ingest them, just with NULL FKs. The lore-path filters on these to
only embed blurbs we successfully resolved.

`build_db.py` does NOT touch this table. Its wipe-and-rebuild for shows /
performances stays as-is. If `build_db.py` ever drops the entire DB file,
the user would need to re-run `build_headyversion.py` to repopulate;
acknowledge this in the script's docstring but don't try to coordinate.

---

## `lore/fetchers/headyversion.py` — the scraper

```python
"""HeadyVersion scraper: song index -> song pages -> submissions.

Used by BOTH:
  - build_headyversion.py (structured: writes community_votes in dead.db)
  - the lore Fetcher interface (writes top-N blurbs to dead_lore.db)

stdlib only. Respects robots.txt: never fetches /s2s/comments/.
"""
import re
import time
import urllib.request
from dataclasses import dataclass
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
    r'href="(/song/(\d+)/grateful-dead/([^/]+)/)"'
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


# ---------- discovery ----------

@dataclass(frozen=True)
class SongLink:
    heady_song_id: int
    slug: str
    url: str          # absolute


def discover_songs() -> list[SongLink]:
    """Single fetch of /search/all/?order=count. Returns ~375 song URLs."""
    _, _, body = _http_get(INDEX_URL)
    seen: set[int] = set()
    out: list[SongLink] = []
    for m in SONG_LINK_RE.finditer(body):
        rel, sid_s, slug = m.group(1), m.group(2), m.group(3)
        sid = int(sid_s)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(SongLink(heady_song_id=sid, slug=slug, url=urljoin(BASE, rel)))
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
    return t.group(1).strip() if t else None


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

def fetch_song_submissions(song_url: str) -> list[HVSubmission]:
    """Walk ?page=1, ?page=2, ... until a page yields no new submissions."""
    seen: set[int] = set()
    out: list[HVSubmission] = []
    page = 1
    while True:
        url = f"{song_url}?page={page}" if page > 1 else song_url
        status, _, body = _http_get(url)
        time.sleep(REQUEST_PAUSE_S)
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
        if page > 50:   # hard safety stop; Eyes is ~24 pages, far more is suspect
            break
    return out


VENUE_RE = re.compile(
    r"<title>([^|]+?)\s*\|\s*headyversion", re.IGNORECASE
)


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

def iter_submissions(*, fetch_show_meta: bool = True) -> Iterator[HVSubmission]:
    """Yield every submission across every song. Optionally enrich with
    show venue / archive_id (extra requests; default ON for the structured
    build, OFF for spot-checks)."""
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
                # archive_id is on the submission for the structured table;
                # attached via a sidecar attribute since dataclass is frozen=False
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
        from collections import defaultdict
        by_song: dict[int, list[HVSubmission]] = defaultdict(list)
        # iter without show metadata first for speed in the lore path
        for s in iter_submissions(fetch_show_meta=True):
            by_song[s.heady_song_id].append(s)

        for heady_song_id, subs in by_song.items():
            subs.sort(key=lambda s: s.vote_score, reverse=True)
            top = subs[:TOP_BLURBS_PER_SONG]
            for rank, s in enumerate(top, start=1):
                if not s.blurb or not s.song_name:
                    continue
                title = f"{s.song_name} — {s.show_date or 'unknown date'}"
                text = (
                    f"[HeadyVersion top {rank} for {s.song_name}, "
                    f"{s.show_date or 'date unknown'}"
                    + (f" at {s.venue}" if s.venue else "")
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
                        "venue": s.venue,
                        "vote_score": s.vote_score,
                        "rank_within_song": rank,
                        "archive_id": getattr(s, "archive_id", None),
                        "heady_song_id": heady_song_id,
                    },
                )
```

Implementer notes:
- The submission "window slice" via `_slice_submissions` is a deliberate
  alternative to a full HTML parse — it's robust to template churn as long
  as the score div remains the anchor. Do NOT switch to bs4 / lxml.
- `iter_submissions` enriches with show metadata via a cache to avoid
  refetching `/show/<id>/` for shows that span many songs. Most shows
  appear under multiple songs (every song the band played that night).
- The `archive_id` is attached via attribute assignment on the dataclass.
  If you'd rather not mutate frozen-ish dataclasses, convert HVSubmission
  to use `field(default=None)` for `archive_id` and use a normal field —
  either is fine, do not add another wrapper class.
- The `fetch_song_submissions` safety stop at page=50 is paranoia; Eyes
  Of The World caps at ~24 pages, and we'd want to know if any song
  exceeded that.

---

## `build_headyversion.py` — top-level structured-table builder

```python
"""Build the community_votes table in dead.db from HeadyVersion.

Runnable: python3 -m build_headyversion

Idempotent. Creates table if absent. Upserts on submission_id.
Does NOT touch build_db.py's tables. If build_db.py wipes dead.db
entirely, re-run this script to repopulate.
"""
import os
import sqlite3
from datetime import datetime, timezone

from lore.fetchers.headyversion import iter_submissions
from lore.song_matcher import _load_canon

DEAD_DB_PATH = os.environ.get("DEAD_DB_PATH", "/hddpool/datastore/dead.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS community_votes (
    submission_id    INTEGER PRIMARY KEY,
    heady_song_id    INTEGER NOT NULL,
    song_uuid        TEXT,
    song_name        TEXT NOT NULL,
    show_date        TEXT,
    venue            TEXT,
    city             TEXT,
    vote_score       INTEGER NOT NULL,
    blurb            TEXT,
    archive_id       TEXT,
    heady_url        TEXT NOT NULL,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cv_song_uuid    ON community_votes(song_uuid);
CREATE INDEX IF NOT EXISTS idx_cv_show_date    ON community_votes(show_date);
CREATE INDEX IF NOT EXISTS idx_cv_vote_score   ON community_votes(vote_score DESC);
CREATE INDEX IF NOT EXISTS idx_cv_heady_song   ON community_votes(heady_song_id);
"""


def _build_name_to_uuid_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Lowercase canonical name -> uuid. Used to resolve HV names."""
    out: dict[str, str] = {}
    for name, uuid in conn.execute("SELECT name, uuid FROM songs"):
        out[name.lower()] = uuid
    return out


def _resolve_song_uuid(hv_name: str, name_map: dict[str, str]) -> str | None:
    """Try exact, then case-insensitive, then apostrophe-fold."""
    if not hv_name:
        return None
    key = hv_name.lower()
    if key in name_map:
        return name_map[key]
    folded = key.replace("\u2019", "'").replace("'", "")
    for n_lower, uuid in name_map.items():
        if n_lower.replace("\u2019", "'").replace("'", "") == folded:
            return uuid
    return None


def main() -> int:
    conn = sqlite3.connect(DEAD_DB_PATH)
    conn.executescript(SCHEMA)
    name_map = _build_name_to_uuid_map(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    unresolved_names: dict[str, int] = {}
    n_subs = n_resolved = 0

    cur = conn.cursor()
    for s in iter_submissions(fetch_show_meta=True):
        uuid = _resolve_song_uuid(s.song_name, name_map)
        if uuid:
            n_resolved += 1
        else:
            unresolved_names[s.song_name] = unresolved_names.get(s.song_name, 0) + 1
        cur.execute("""
            INSERT INTO community_votes(
                submission_id, heady_song_id, song_uuid, song_name,
                show_date, venue, city, vote_score, blurb, archive_id,
                heady_url, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(submission_id) DO UPDATE SET
                heady_song_id=excluded.heady_song_id,
                song_uuid=excluded.song_uuid,
                song_name=excluded.song_name,
                show_date=excluded.show_date,
                venue=excluded.venue,
                city=excluded.city,
                vote_score=excluded.vote_score,
                blurb=excluded.blurb,
                archive_id=excluded.archive_id,
                heady_url=excluded.heady_url,
                fetched_at=excluded.fetched_at
        """, (
            s.submission_id, s.heady_song_id, uuid, s.song_name,
            s.show_date, s.venue, s.city, s.vote_score, s.blurb,
            getattr(s, "archive_id", None), s.submission_url, now,
        ))
        n_subs += 1
        if n_subs % 200 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    print(f"[hv] wrote {n_subs} submissions, resolved {n_resolved} to song_uuid")
    if unresolved_names:
        # write the unresolved log for the user to inspect
        from pathlib import Path
        log = Path(__file__).parent / "headyversion_unresolved_songs.log"
        lines = [f"{n}\t{name}" for name, n in
                 sorted(unresolved_names.items(), key=lambda kv: -kv[1])]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[hv] {len(unresolved_names)} unresolved song names "
              f"({sum(unresolved_names.values())} rows) -> {log.name}")

    # validation floor — fail loud on suspiciously thin results
    assert n_subs >= 5000, f"expected >=5000 submissions, got {n_subs}"
    assert n_resolved >= int(n_subs * 0.8), (
        f"only {n_resolved}/{n_subs} resolved to song_uuid; "
        "song name mapping is broken"
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

---

## Lore-path build entry

Add one tiny runnable so the user can rebuild blurb chunks independently:

```python
# lore/build_headyversion_lore.py
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
```

---

## New MCP tools in `homelab-mcp/tools/deaddb.py`

Two phase-2-style structured tools, written in the same style as the
existing 11 (decorator, return shape, etc — match exactly).

```python
@<existing_mcp_tool_decorator>
def dead_top_versions(song_name: str, k: int = 10) -> dict:
    """Top community-voted versions of a Grateful Dead song.

    Pulls from the HeadyVersion community_votes table joined to your
    canonical shows/performances. Returns the highest-voted submissions
    with date, venue, blurb, vote count, and archive.org identifier
    when available.

    Args:
        song_name: Canonical song name as in dead.db.songs.name. Case-
                   insensitive; alias resolution via song_matcher.
        k: Number of top versions to return (default 10, max 50).
    """
    import sqlite3
    from lore.song_matcher import _gazetteer
    table, _, _ = _gazetteer()
    name_lower = song_name.lower()
    canonical = table.get(name_lower, (song_name, False))[0]

    k = max(1, min(int(k), 50))
    conn = sqlite3.connect(f"file:{DEAD_DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT cv.show_date, cv.venue, cv.city, cv.vote_score, cv.blurb,
               cv.archive_id, cv.heady_url,
               s.name AS song_canonical
        FROM community_votes cv
        LEFT JOIN songs s ON s.uuid = cv.song_uuid
        WHERE LOWER(s.name) = LOWER(?) OR LOWER(cv.song_name) = LOWER(?)
        ORDER BY cv.vote_score DESC
        LIMIT ?
    """, (canonical, song_name, k)).fetchall()
    conn.close()
    return {
        "song": canonical,
        "versions": [
            {"date": r[0], "venue": r[1], "city": r[2],
             "vote_score": r[3], "blurb": r[4],
             "archive_id": r[5], "heady_url": r[6]}
            for r in rows
        ],
    }


@<existing_mcp_tool_decorator>
def dead_show_votes(date: str) -> dict:
    """Community-voted submissions from a single show.

    Returns every HeadyVersion submission for the given date, sorted by
    vote score. Useful for "what stood out about <date>" questions.

    Args:
        date: Show date in YYYY-MM-DD format.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{DEAD_DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT cv.song_name, cv.vote_score, cv.blurb,
               cv.archive_id, cv.heady_url, cv.venue, cv.city
        FROM community_votes cv
        WHERE cv.show_date = ?
        ORDER BY cv.vote_score DESC
    """, (date,)).fetchall()
    # also grab show metadata if it exists
    show_row = conn.execute("""
        SELECT venue, city, state, country
        FROM shows WHERE date = ?
    """, (date,)).fetchone()
    conn.close()
    return {
        "date": date,
        "show": ({"venue": show_row[0], "city": show_row[1],
                  "state": show_row[2], "country": show_row[3]}
                 if show_row else None),
        "votes": [
            {"song": r[0], "vote_score": r[1], "blurb": r[2],
             "archive_id": r[3], "heady_url": r[4],
             "venue": r[5], "city": r[6]}
            for r in rows
        ],
    }
```

---

## `lore/router.py` — extend followup-suggestions table

Add the two new tools to the router's followup hints so `dead_ask`
surfaces them whenever a song or date is extracted:

```python
# add to _FOLLOWUP_BY_SONG (existing list):
_FOLLOWUP_BY_SONG.append(
    ("dead_top_versions", lambda s: {"name": s},
     "Top community-voted versions of this song from HeadyVersion")
)

# add to _FOLLOWUP_BY_DATE (existing list):
_FOLLOWUP_BY_DATE.append(
    ("dead_show_votes", lambda d: {"date": d},
     "Community-voted submissions from this show")
)
```

This is the only edit to `router.py`. No other logic changes.

---

## Success criteria

1. All new files exist:
   - `lore/fetchers/headyversion.py`
   - `build_headyversion.py` (top-level)
   - `lore/build_headyversion_lore.py`
2. `homelab-mcp/tools/deaddb.py` has `dead_top_versions` and
   `dead_show_votes` registered alongside the existing 13 tools (11
   phase-2 + 2 phase-3). Style matches the existing tools exactly.
3. `lore/router.py` has the two new entries in the followup-hint tables.
4. Smoke tests still pass unchanged:
   - `python3 -m lore.smoke_test` -> "OK: 3 docs..."
   - Existing extract_entities tests still pass.
5. Recon-validating smoke test, BEFORE the full structured build:
   ```python
   from lore.fetchers.headyversion import discover_songs, fetch_song_submissions
   songs = discover_songs()
   assert 300 <= len(songs) <= 450, f"expected ~375 songs, got {len(songs)}"
   eyes = next(s for s in songs if "eyes-of-the-world" in s.slug)
   subs = fetch_song_submissions(eyes.url)
   assert len(subs) >= 100, f"expected >=100 Eyes submissions, got {len(subs)}"
   # vote scores should look sane and sortable
   scores = sorted((s.vote_score for s in subs), reverse=True)
   assert scores[0] >= 100, f"top Eyes vote should be >=100, got {scores[0]}"
   # show dates should mostly be present and ISO-formatted
   dated = sum(1 for s in subs if s.show_date)
   assert dated >= int(len(subs) * 0.9), \
       f"only {dated}/{len(subs)} subs had show_date"
   ```
   Show me this output before running the full build.
6. `python3 -m build_headyversion` runs, ingests >=5000 submissions, >=80%
   resolved to song_uuid, writes `headyversion_unresolved_songs.log`. Show
   me:
   - The total submissions / resolved counts
   - The top 20 lines of the unresolved log
   - `SELECT COUNT(*), COUNT(DISTINCT song_uuid), COUNT(DISTINCT show_date)
      FROM community_votes;`
7. `python3 -m lore.build_headyversion_lore` runs, ingests >=1000 blurb
   chunks. Then a spot check:
   ```python
   from lore.query import search
   search("Eyes of the world 8/6/74 Roosevelt Stadium", k=3)
   # at least one source='headyversion' chunk should appear, with the
   # 1974-08-06 Roosevelt Stadium version's blurb
   ```
8. MCP smoke tests after restarting homelab-mcp:
   ```
   dead_top_versions(song_name="Eyes of the World", k=5)
   # expect 5 versions, top score the Roosevelt 8/6/74 version

   dead_show_votes(date="1977-05-08")
   # expect all Cornell 77 votes, sorted by score, with show metadata
   ```
9. `dead_ask("what's the best version of Eyes of the World")` returns
   suggested_followups including BOTH `dead_song_history` AND
   `dead_top_versions` for Eyes of the World.
10. No git commit — user reviews diff and commits.

---

## Out of scope

- Comment ingestion. Disallowed by robots.txt; respect it.
- Cross-corpus dedup (HV blurbs vs LIA chunks that quote them, etc.).
  Tiny effect at this scale.
- Incremental sync. Re-running both build scripts is the update path.
  At ~25 min total it's fine to run monthly.
- Tracking individual voters / commenter usernames. Out of scope and
  also unkind to scrape.
- An MCP tool that returns a full song's vote distribution. Don't add
  it unless asked.
- Optimizing the lore-path to read from community_votes instead of
  re-scraping. Future optimization, ~25 min savings, not worth the
  coupling now.

---

## Notes for the implementer

- Read this file plus lore/SPEC.md and lore/SPEC_mcp_tools.md before coding.
- Respect robots.txt. Never fetch /s2s/comments/. Comments are not part
  of this build.
- Two build scripts on purpose — structured and lore are independent
  pipelines that happen to share scraper code. Don't merge them.
- Song-name resolution to dead.db.songs.uuid is load-bearing. The
  validation floor (>=80% resolved) catches a broken name map early.
  If you fall below 80%, STOP and show me the unresolved log — likely
  the resolver needs an alias fallback against song_aliases.txt.
- The 1.0 s pause between fetches is non-negotiable. This is a small
  community site running on jQuery 1.7 from 2011. Do not parallelize.
- stdlib only. urllib, re, html.parser via _html.py, sqlite3. If you
  feel the urge to add a dependency, STOP and ask.
- Match existing dead-db style: terse docstrings, stdlib over deps.

---

## ADDENDUM (post-recon-2): drop `fetch_show_meta`, source venue/archive from existing `dead.db` tables

**Status:** authoritative — supersedes the original `fetch_show_meta=True`
flow wherever they conflict. Original text above is preserved for context;
this section overrides it.

### What we found running the build

The original spec turned on `fetch_show_meta=True` in `iter_submissions`,
grabbing venue/city and the archive.org identifier from two extra HTTP
requests per unique show (`/show/<id>/` and `/show/<id>/archive/`). Recon
sanity-checked the song pages but did not measure show-page latency. In
practice the show endpoints respond slowly and frequently hit the 30s
urllib timeout — popular songs reference 300-500 distinct shows, and the
build stretched from the estimated ~25 minutes into hours per popular song.
Untenable.

### The structural fix

The data we were paying HTTP for is **already in `dead.db` canonically**.
Venue + city live on `shows`. Archive identifiers live on
`archive_recordings`. HeadyVersion's per-show pages are a third copy of
information we already own.

The corrected design sources venue/city/archive at *query time* by JOINing
the `community_votes` table to the existing canonical tables. The HV scrape
does not need to fetch them at all.

### Schema change — drop `archive_id` from `community_votes`

The `archive_id` column comes out. JOIN to `archive_recordings` at query
time in the MCP tools. Per-submission archive_id was always per-show data
in disguise — every submission for the same show points at the same
recording — so the right home is `archive_recordings`, not
`community_votes`. Updated schema:

```sql
CREATE TABLE IF NOT EXISTS community_votes (
    submission_id    INTEGER PRIMARY KEY,
    heady_song_id    INTEGER NOT NULL,
    song_uuid        TEXT,
    song_name        TEXT NOT NULL,
    show_date        TEXT,
    venue            TEXT,                           -- kept, nullable, will be NULL after this change
    city             TEXT,                           -- ditto
    vote_score       INTEGER NOT NULL,
    blurb            TEXT,
    heady_url        TEXT NOT NULL,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cv_song_uuid    ON community_votes(song_uuid);
CREATE INDEX IF NOT EXISTS idx_cv_show_date    ON community_votes(show_date);
CREATE INDEX IF NOT EXISTS idx_cv_vote_score   ON community_votes(vote_score DESC);
CREATE INDEX IF NOT EXISTS idx_cv_heady_song   ON community_votes(heady_song_id);
```

`venue` and `city` stay in the schema (nullable, will be NULL post-fix)
so the table shape remains stable if a future build wants to populate
them from a different source. No migration script — `CREATE TABLE IF NOT
EXISTS` plus the user dropping the existing `community_votes` row before
the re-run is the cleanup path.

### Build-script change — `fetch_show_meta=False`

In `build_headyversion.py`, the `iter_submissions(...)` call passes
`fetch_show_meta=False`. The INSERT no longer references `archive_id`
(column dropped). Venue and city are written as the NULLs that
`HVSubmission` carries by default.

The `fetch_show_metadata()` function in `lore/fetchers/headyversion.py`
remains in the file but is unused by the structured-build path. Leave it
in place — it's a small amount of code, harmless to retain, and useful
reference if a future need arises. Mark it `# unused as of addendum;
show metadata now sourced via JOIN in MCP tools`.

### `dead_top_versions` — JOIN venue/city/archive at query time

```python
@<existing_mcp_tool_decorator>
def dead_top_versions(song_name: str, k: int = 10) -> dict:
    """Top community-voted versions of a Grateful Dead song.

    Joins community_votes (HeadyVersion) to canonical shows + archive
    recordings for venue, city, and archive.org identifiers.
    """
    import sqlite3
    from lore.song_matcher import _gazetteer
    table, _, _ = _gazetteer()
    name_lower = song_name.lower()
    canonical = table.get(name_lower, (song_name, False))[0]

    k = max(1, min(int(k), 50))
    conn = sqlite3.connect(f"file:{DEAD_DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT cv.show_date,
               COALESCE(s.venue, cv.venue)             AS venue,
               COALESCE(s.city, cv.city)               AS city,
               s.state, s.country,
               cv.vote_score, cv.blurb, cv.heady_url,
               ar.identifier                            AS archive_id,
               sng.name                                 AS song_canonical
        FROM community_votes cv
        LEFT JOIN songs   sng ON sng.uuid = cv.song_uuid
        LEFT JOIN shows   s   ON s.date   = cv.show_date
        LEFT JOIN archive_recordings ar ON ar.show_date = cv.show_date
        WHERE LOWER(sng.name) = LOWER(?) OR LOWER(cv.song_name) = LOWER(?)
        ORDER BY cv.vote_score DESC, ar.avg_rating DESC NULLS LAST
        LIMIT ?
    """, (canonical, song_name, k)).fetchall()
    conn.close()
    return {
        "song": canonical,
        "versions": [
            {"date": r[0], "venue": r[1], "city": r[2],
             "state": r[3], "country": r[4],
             "vote_score": r[5], "blurb": r[6], "heady_url": r[7],
             "archive_id": r[8]}
            for r in rows
        ],
    }
```

Notes:
- `LEFT JOIN archive_recordings` may produce duplicate rows when a show
  has multiple recordings. If `archive_recordings` has more than one row
  per `show_date` in your data, the `LIMIT ?` cap could surface the same
  vote multiple times. Verify the join cardinality after the build — if
  duplicates appear, switch to a correlated subquery picking the
  highest-rated recording per date.
- `NULLS LAST` keeps versions without an archive recording from sorting
  to the top when archive ratings tie at NULL.

### `dead_show_votes` — same JOIN pattern

```python
@<existing_mcp_tool_decorator>
def dead_show_votes(date: str) -> dict:
    """Community-voted submissions from a single show, with canonical
    venue/city and archive recordings JOINed in."""
    import sqlite3
    conn = sqlite3.connect(f"file:{DEAD_DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT cv.song_name, cv.vote_score, cv.blurb, cv.heady_url
        FROM community_votes cv
        WHERE cv.show_date = ?
        ORDER BY cv.vote_score DESC
    """, (date,)).fetchall()
    show_row = conn.execute("""
        SELECT venue, city, state, country
        FROM shows WHERE date = ?
    """, (date,)).fetchone()
    archive_rows = conn.execute("""
        SELECT identifier, avg_rating, num_reviews
        FROM archive_recordings WHERE show_date = ?
        ORDER BY avg_rating DESC NULLS LAST
    """, (date,)).fetchall()
    conn.close()
    return {
        "date": date,
        "show": ({"venue": show_row[0], "city": show_row[1],
                  "state": show_row[2], "country": show_row[3]}
                 if show_row else None),
        "archive_recordings": [
            {"identifier": r[0], "avg_rating": r[1], "num_reviews": r[2]}
            for r in archive_rows
        ],
        "votes": [
            {"song": r[0], "vote_score": r[1], "blurb": r[2],
             "heady_url": r[3]}
            for r in rows
        ],
    }
```

### Lore-path — populate `documents.metadata` venue/city via `dead.db` lookup

`HeadyVersionFetcher.fetch()` no longer relies on per-submission scraped
venue/city. Instead, open a read-only `dead.db` connection at the start of
the fetch, build a `{show_date: (venue, city)}` cache from `shows` once,
and stuff venue/city into each blurb's metadata from there. One SQL query
at fetcher init, zero HTTP for show metadata. Sketch:

```python
# at the top of HeadyVersionFetcher.fetch(), before the iter_submissions loop:
import sqlite3
dead_db = os.environ.get("DEAD_DB_PATH", "/hddpool/datastore/dead.db")
show_meta: dict[str, tuple[str | None, str | None]] = {}
with sqlite3.connect(f"file:{dead_db}?mode=ro", uri=True) as cx:
    for d, v, c in cx.execute("SELECT date, venue, city FROM shows"):
        show_meta[d] = (v, c)
# ...then inside the loop, when building the metadata dict:
v, c = show_meta.get(s.show_date or "", (None, None))
metadata={
    "song": s.song_name,
    "show_date": s.show_date,
    "venue": v,
    "city": c,
    "vote_score": s.vote_score,
    "rank_within_song": rank,
    "heady_song_id": heady_song_id,
    # archive_id no longer carried; query archive_recordings if needed
}
```

The `iter_submissions(...)` call inside `HeadyVersionFetcher.fetch()` also
changes to `fetch_show_meta=False` for consistency with the structured
build.

### Safety-stop fix in `fetch_song_submissions`

The original `page > 50` hard stop was paranoia for a build that no longer
takes hours. Raise to `page > 100`. The real terminator is the
"no new submission IDs on this page" break already in the loop; the
page cap is belt-and-suspenders against a parser bug, not a normal
stopping condition. Silently truncating at 50 was a real risk for any
song with >750 submissions (50 pages × 15 per page).

### What to re-run

1. Drop the existing `community_votes` table:
   ```sql
   DROP TABLE IF EXISTS community_votes;
   ```
2. Re-run `python3 -m build_headyversion`. With `fetch_show_meta=False`
   it should complete in roughly the originally-estimated ~25 minutes.
3. The lore-path corpus (`headyversion` source in `dead_lore.db`) was
   built against the old fetcher; rebuild it after this change so the
   metadata is consistent:
   ```sql
   DELETE FROM documents WHERE source = 'headyversion';
   ```
   then `python3 -m lore.build_headyversion_lore`.
4. Verify the two MCP tools return venue/city populated (from JOIN) and
   that `dead_top_versions` returns archive_id for shows with recordings.

### Lesson worth keeping

The original spec made a quiet assumption: that show-page latency would
be similar to song-page latency. Recon checked the latter, not the
former. For future scraper specs: when the build will touch a new
endpoint type, recon needs to time at least one fetch from each
endpoint, not just confirm it returns HTML.
