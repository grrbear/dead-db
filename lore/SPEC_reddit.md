# dead-db phase 3 — Reddit fetcher (corpus #6)

## Context

Sixth real corpus for the phase 3 lore RAG. Adds fan discussion from
r/gratefuldead: show reviews, "I was there" accounts, song/segue analysis,
recording-source debates — first-person lore that exists nowhere in the
essay/book/wiki corpora.

Read `lore/SPEC.md` (scaffolding), `lore/SPEC_lia.md` (web-source fetcher,
the closest analog), and `lore/SPEC_books.md` (metadata-column usage) before
this file. This spec assumes all of phase 3 + phase 5 are implemented and
committed (CLAUDE.md shows them complete).

The value and the risk are the same fact: Reddit is mostly noise. A good
fetcher is 80% quality filtering. A naive one floods the index with ticket-stub
photos and one-line "🔥" comments and degrades every other source's retrieval.

---

## Locked decisions — do not relitigate

Carried from earlier phases: sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU;
separate dead_lore.db; raw_text stored in documents; code in dead-db/lore/;
stdlib only (urllib, no requests, no praw); idempotent ingest at
(source, source_id) grain; song matching runs automatically in normalize.py;
`published` is the POST date, never the date of events discussed.

New for this phase:

- **One subreddit for v1: r/gratefuldead.** `source="reddit"` (single value,
  not per-subreddit like lia's two sources). The subreddit goes in the
  `metadata` JSON column, not the source value. A `SUBREDDITS` tuple makes
  adding more a one-line change later — but v1 ships one.
- **One submission = one document.** `source_id` = the post permalink
  (`/r/gratefuldead/comments/<id>/<slug>/`) — stable, human-readable, matches
  the URL-as-source_id convention from lia. `raw_text` = post title + selftext
  + selected top comments, folded into readable plain text.
- **Unauthenticated public `.json`, mirroring reddit-mcp.** No app, no creds,
  no OAuth. The official-API path was abandoned: Reddit moved app creation
  behind the "Responsible Builder Policy" approval gate (self-serve script-app
  registration no longer works), and it isn't worth chasing for a personal
  homelab indexer. reddit-mcp already proves unauthenticated `.json` works from
  arrstack. Reads hit `https://www.reddit.com/...<endpoint>.json` with a
  descriptive `User-Agent` — Reddit blocks generic/empty UAs; this is the
  single most important header. Tradeoff vs OAuth: anonymous traffic is
  throttled harder, so the build is a slow, run-it-once(-ish) job, not a 15-min
  one. Mitigated by 2s pacing + exponential backoff on 429/503, and the crawl
  is idempotent (re-run resumes — upsert on permalink, dedup by id), so a
  throttle-abort loses no work.
- **Not routed through reddit-mcp (rejected).** reddit-mcp is a Node/TS
  *interactive* MCP server whose output is truncated/formatted for an LLM
  context window — lossy for ingest, and a wrong-language, wrong-transport
  dependency for a batch ETL job. This fetcher talks to Reddit directly (stdlib
  urllib) for raw, ungated data. The two share nothing now that there are no
  OAuth creds to reuse. Do not refactor this to call reddit-mcp.
- **Quality filtering is the spec.** Two gates, both env-tunable:
  - *Post gate:* keep a post only if `score >= MIN_POST_SCORE` (default 10)
    AND (`len(selftext) >= MIN_SELFTEXT` (default 200) OR it has at least
    `MIN_GOOD_COMMENTS` (default 3) qualifying comments). Skip `stickied`
    posts, `over_18`, and pure link/image posts with no discussion.
  - *Comment gate:* keep a comment if `score >= MIN_COMMENT_SCORE` (default 5)
    AND `len(body) >= MIN_COMMENT_LEN` (default 120) AND author is not
    `AutoModerator`/deleted AND body is not `[deleted]`/`[removed]`. Take the
    top `MAX_COMMENTS_PER_POST` (default 40) by score, flattened (threading
    discarded — fine for embedding).
- **Slash-date annotation in the fetcher.** Redditers write "5/8/77", not
  "1977-05-08". normalize.py's `DATE_RE` only catches ISO, so without help
  `mentioned_dates` would be empty for nearly all Reddit chunks — killing the
  date-entity hard-filter in `dead_ask` for exactly the content where date
  filtering matters most. Fix: the fetcher annotates each loose date inline
  with its ISO form — "5/8/77" -> "5/8/77 [1977-05-08]" — so the existing ISO
  regex picks it up in whatever chunk it lands in. The loose-date logic is
  copied locally into the fetcher (consistent with `DATE_RE` already being
  duplicated between normalize.py and router.py — this codebase tolerates that
  duplication; do NOT refactor a shared module this phase).
- **Comment "more" stubs are not expanded.** `kind="more"` nodes (Reddit's
  "load more comments") are ignored. One comments fetch per post, top-sorted,
  bounded. No recursive expansion.
- **Router cap for reddit = 2.** Add `"reddit": 2` to `PER_SOURCE_CAP` in
  router.py. Reddit is lower-authority and noisier than essays/books; cap it
  below them so it supplements rather than dominates answers. This is the ONLY
  edit to a file outside the three new reddit files.

---

## Reddit `.json` mechanics (reference for implementer)

No auth. Every read is an HTTP GET to `https://www.reddit.com<path>.json` with
headers `User-Agent: <descriptive>` and `Accept: application/json`.

- **Listing:** `GET /r/gratefuldead/top.json?t=all&limit=100&after=<fullname>`.
  Walk `after` until null or ~1000 items (Reddit's hard listing ceiling). Then
  a second pass with `t=year` to catch recent high-value posts on re-runs.
  Dedup by post id across passes.
  - `data.children[].data`: `id`, `name` (`t3_…`), `permalink`, `title`,
    `selftext`, `score`, `num_comments`, `link_flair_text`, `author`,
    `created_utc`, `is_self`, `over_18`, `stickied`.
  - `data.after`: pagination cursor.
- **Comments:** `GET /comments/<id>.json?sort=top&limit=200&depth=10`. Returns
  a 2-element array: `[post_listing, comments_listing]`. Walk
  `comments_listing.data.children`; `kind="t1"` = comment (recurse
  `data.replies` if it's a listing dict), `kind="more"` = skip.

**Politeness / throttling:** anonymous traffic is rate-limited and server IPs
are watched. Pace 2.0s between every call, sequential only. On HTTP 429/503,
exponential backoff (respect `Retry-After` if present, else doubling delay),
up to 4 attempts, then raise. A descriptive `User-Agent` is mandatory — a
generic/empty one is the fastest way to get blocked. Each post = 1 listing slot
+ 1 comments fetch; ~1000 posts at this pacing ≈ ~1 hour. Run it once; re-runs
are cheap and idempotent.

**If arrstack's IP gets hard-blocked mid-crawl:** the backoff handles short
throttles. For a sustained block, route the one-off build through the `gluetun`
VPN egress already in the stack, or run it from a Mac. Do NOT pre-build that —
only if it actually 429s past the retries.

---

## `lore/fetchers/reddit.py` — the fetcher

```python
"""r/gratefuldead fetcher. Unauthenticated public .json (mirrors reddit-mcp),
top-listing discovery, per-post comment fetch with score/length quality gates.
Folds post + top comments into one document. Annotates slash-dates with ISO so
normalize.py's date regex catches them. stdlib only (urllib), no praw, no creds.
"""
import os
import re
import time
import json
import urllib.parse
import urllib.request
from typing import Iterator

from ._base import Fetcher, RawDocument

SUBREDDITS = ("gratefuldead",)        # v1: one. Add siblings here later.
BASE = "https://www.reddit.com"
USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "dead-db-lore/0.1 (homelab personal project by /u/YOUR_REDDIT_USERNAME)",
)
REQUEST_PAUSE_S = 2.0                  # anonymous traffic is throttled; pace politely
MAX_RETRIES = 4
LISTING_PAGE = 100
LISTING_CEILING = 1000                 # Reddit's hard cap per listing
TIME_WINDOWS = ("all", "year")         # two passes, dedup by id

# quality gates (env-tunable)
MIN_POST_SCORE        = int(os.environ.get("REDDIT_MIN_POST_SCORE", "10"))
MIN_SELFTEXT          = int(os.environ.get("REDDIT_MIN_SELFTEXT", "200"))
MIN_GOOD_COMMENTS     = int(os.environ.get("REDDIT_MIN_GOOD_COMMENTS", "3"))
MIN_COMMENT_SCORE     = int(os.environ.get("REDDIT_MIN_COMMENT_SCORE", "5"))
MIN_COMMENT_LEN       = int(os.environ.get("REDDIT_MIN_COMMENT_LEN", "120"))
MAX_COMMENTS_PER_POST = int(os.environ.get("REDDIT_MAX_COMMENTS", "40"))

_SKIP_AUTHORS = {"AutoModerator", "[deleted]", None, ""}
_DEAD_BODIES = {"[deleted]", "[removed]", ""}

# loose-date -> ISO. Copied from router.py on purpose (this codebase already
# duplicates DATE_RE between normalize.py and router.py; no shared module).
LOOSE_DATE_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")


def _loose_to_iso(m: re.Match) -> str | None:
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 1900 + y if y >= 60 else 2000 + y
    if not (1965 <= y <= 1995) or not (1 <= mo <= 12) or not (1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _annotate_dates(text: str) -> str:
    """Append ISO form after each in-band slash-date so DATE_RE catches it."""
    def repl(m: re.Match) -> str:
        iso = _loose_to_iso(m)
        return f"{m.group(0)} [{iso}]" if iso else m.group(0)
    return LOOSE_DATE_RE.sub(repl, text)


class RedditFetcher(Fetcher):
    name = "reddit"

    # ---- http (unauthenticated .json + backoff) ----
    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        delay = REQUEST_PAUSE_S
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < MAX_RETRIES:
                    wait = max(int(e.headers.get("Retry-After", 0) or 0), delay)
                    time.sleep(wait)
                    delay *= 2                  # exponential backoff
                    continue
                raise
        raise RuntimeError(f"reddit: gave up after {MAX_RETRIES} retries on {path}")

    # ---- discovery ----
    def discover(self) -> list[str]:
        ids: list[str] = []
        for sub in SUBREDDITS:
            ids.extend(self._discover_sub(sub))
        return ids

    def _discover_sub(self, sub: str) -> list[str]:
        seen: dict[str, str] = {}   # id -> permalink, dedup across windows
        for t in TIME_WINDOWS:
            after, fetched = None, 0
            while fetched < LISTING_CEILING:
                params = {"t": t, "limit": LISTING_PAGE}
                if after:
                    params["after"] = after
                data = self._get(f"/r/{sub}/top.json", params)["data"]
                for ch in data["children"]:
                    d = ch["data"]
                    seen.setdefault(d["id"], d["permalink"])
                fetched += len(data["children"])
                after = data.get("after")
                time.sleep(REQUEST_PAUSE_S)
                if not after:
                    break
        return list(seen.values())

    # ---- fetch ----
    def fetch(self, source_ids: list[str] | None = None) -> Iterator[RawDocument]:
        for sub in SUBREDDITS:
            yield from self._fetch_sub(sub)

    def _fetch_sub(self, sub: str) -> Iterator[RawDocument]:
        seen: set[str] = set()
        for t in TIME_WINDOWS:
            after, fetched = None, 0
            while fetched < LISTING_CEILING:
                params = {"t": t, "limit": LISTING_PAGE}
                if after:
                    params["after"] = after
                data = self._get(f"/r/{sub}/top.json", params)["data"]
                for ch in data["children"]:
                    post = ch["data"]
                    if post["id"] in seen:
                        continue
                    seen.add(post["id"])
                    doc = self._build_doc(sub, post)
                    if doc:
                        yield doc
                fetched += len(data["children"])
                after = data.get("after")
                time.sleep(REQUEST_PAUSE_S)
                if not after:
                    break

    def _build_doc(self, sub: str, post: dict) -> RawDocument | None:
        if post.get("stickied") or post.get("over_18"):
            return None
        if post.get("score", 0) < MIN_POST_SCORE:
            return None

        selftext = (post.get("selftext") or "").strip()
        comments = self._fetch_comments(post["id"])
        time.sleep(REQUEST_PAUSE_S)

        if len(selftext) < MIN_SELFTEXT and len(comments) < MIN_GOOD_COMMENTS:
            return None

        title = (post.get("title") or "").strip()
        parts = [title]
        if selftext:
            parts.append(selftext)
        if comments:
            parts.append("--- comments ---")
            parts.extend(f"[score {c['score']}] {c['body']}" for c in comments)
        raw_text = _annotate_dates("\n\n".join(parts))

        created = post.get("created_utc")
        published = (time.strftime("%Y-%m-%d", time.gmtime(created))
                     if created else None)

        return RawDocument(
            source="reddit",
            source_id=post["permalink"],
            title=title or post["permalink"],
            url=f"https://www.reddit.com{post['permalink']}",
            published=published,
            raw_text=raw_text,
            sections=None,                       # flat; paragraph-merge chunking
            metadata={
                "subreddit": sub,
                "post_id": post["id"],
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "flair": post.get("link_flair_text"),
                "author": post.get("author"),
                "n_comments_kept": len(comments),
            },
        )

    def _fetch_comments(self, post_id: str) -> list[dict]:
        """Top-sorted, flattened, quality-gated. Returns [{score, body}]."""
        resp = self._get(f"/comments/{post_id}.json",
                         {"sort": "top", "limit": 200, "depth": 10})
        if not isinstance(resp, list) or len(resp) < 2:
            return []
        kept: list[dict] = []
        self._walk_comments(resp[1]["data"]["children"], kept)
        kept.sort(key=lambda c: c["score"], reverse=True)
        return kept[:MAX_COMMENTS_PER_POST]

    def _walk_comments(self, children: list, out: list[dict]) -> None:
        for ch in children:
            if ch.get("kind") != "t1":          # skip "more" stubs
                continue
            d = ch["data"]
            body = (d.get("body") or "").strip()
            if (d.get("score", 0) >= MIN_COMMENT_SCORE
                    and len(body) >= MIN_COMMENT_LEN
                    and body not in _DEAD_BODIES
                    and d.get("author") not in _SKIP_AUTHORS):
                out.append({"score": d["score"], "body": body})
            replies = d.get("replies")
            if isinstance(replies, dict):
                self._walk_comments(replies["data"]["children"], out)
```

Implementer notes:
- `name = "reddit"` is the documents.source value; metadata carries subreddit.
- **Set a real `User-Agent`** (put a reddit username or contact in it). This is
  the single biggest factor in not getting blocked. reddit-mcp's descriptive UA
  is exactly why it has never been throttled.
- Politeness is non-negotiable: 2.0s between every call, sequential, one
  comments fetch per post, no "more" expansion.
- The quality gates are the whole point. If a tuning pass is needed, change the
  env constants — do NOT loosen them inline to hit the doc floor.
- `sections=None` is deliberate: a Reddit post has no heading structure, so
  normalize.py uses its flat paragraph-merge path. Don't synthesize sections.

---

## `lore/build_reddit.py` — runnable entry

```python
"""Build the r/gratefuldead corpus into dead_lore.db.
Run: python3 -m lore.build_reddit"""
from .build_lore_db import ingest
from .fetchers.reddit import RedditFetcher


def main() -> int:
    n_docs, n_chunks = ingest(RedditFetcher())
    print(f"[reddit] ingested {n_docs} docs, {n_chunks} chunks")
    assert n_docs >= 30, f"expected >=30 quality docs, got {n_docs}"
    assert n_chunks >= n_docs, f"expected >=1 chunk/doc, got {n_chunks}"
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

Floor is deliberately conservative (30) — post-filter yield from one
subreddit's top-1000+ is uncertain; a low floor avoids false failure. Expect a
few hundred in practice. If a throttle aborts the run before the floor, re-run:
ingest upserts on permalink, so completed docs are skipped cheaply and the
crawl effectively resumes.

---

## Router edit (the one out-of-file change)

In `lore/router.py`, add reddit to the cap dict:

```python
PER_SOURCE_CAP = {
    "lia_essays": 3,
    "wikipedia": 2,
    "book": 4,
    "deadcast": 4,
    "reddit": 2,        # noisier/lower-authority: supplement, don't dominate
}
```

Nothing else in router.py changes.

---

## Success criteria

1. `lore/fetchers/reddit.py` and `lore/build_reddit.py` exist; the `"reddit": 2`
   line is added to `PER_SOURCE_CAP` in router.py. No other files touched.
2. Existing smoke test still passes unchanged (`python3 -m lore.smoke_test`
   -> "OK: 3 docs…").
3. `python3 -m lore.build_reddit` runs end-to-end (no creds needed), ingests
   >=30 docs, prints doc/chunk counts. Expect a ~1-hour run at 2s pacing.
4. Spot-check in a REPL (sanity, not asserts):
   ```python
   from lore.query import search
   search("was anyone at Cornell 5/8/77 what was it like", k=5)
   search("best Dark Star Morning Dew firsthand account", k=5)
   ```
   Reddit docs should surface for first-person/opinion queries. Verify
   retrieved text is real discussion prose — NO bot comments, NO one-liners,
   NO "[deleted]".
5. Verify date annotation worked: confirm a meaningful fraction of reddit
   chunks have a populated `mentioned_dates` (proves slash->ISO annotation
   survived chunking):
   ```python
   import sqlite3
   c = sqlite3.connect("/hddpool/datastore/dead_lore.db")
   tot, dated = c.execute(
       "SELECT COUNT(*), SUM(mentioned_dates != '[]') FROM chunks "
       "JOIN documents d ON d.id = chunks.document_id WHERE d.source='reddit'"
   ).fetchone()
   print(dated, "/", tot, "reddit chunks carry dates")
   ```
6. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- More subreddits (r/gdead, r/gratefuldeadbootlegs) — `SUBREDDITS` tuple makes
  it a one-line add later; ship one now.
- The official Reddit OAuth API — abandoned (Responsible Builder Policy gate).
  If Reddit ever reopens self-serve script apps, switching back is a localized
  change to `_get` + a token helper; not now.
- `search`-based deep coverage of specific shows/songs — listings only this
  phase.
- Recursive "more comments" expansion — bounded to one fetch per post.
- Comment threading / reply context preservation — flattened by score.
- Refactoring loose-date logic into a shared module — duplicated locally on
  purpose, consistent with existing DATE_RE duplication.
- Incremental "only new posts since last run" — ingest() is idempotent on
  permalink; re-running re-walks top listings (cheap at this scale).
- MCP tool changes — `dead_lore`/`dead_ask` pick up `source="reddit"`
  automatically; no tool signature changes.
- praw / requests / any new dependency — stdlib urllib only. If tempted, STOP
  and ask.

---

## Notes for the implementer

- Read this file + SPEC.md + SPEC_lia.md before coding.
- Biggest quality risk is filter leakage: bot comments, deleted bodies, and
  low-effort one-liners. Criterion 4 is the gate — verify before declaring done.
- Second risk is throttling. Keep the 2s pacing and the descriptive User-Agent.
  If the crawl 429s repeatedly even with backoff, STOP — don't lower the pause
  to brute through it. Either route via gluetun/Mac or tell the user; that's a
  decision, not an improvisation.
- Match dead-db style: terse docstrings, stdlib over deps, minimal abstraction.
