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
    "dead-db-lore/0.1 (homelab personal project by /u/GrrGrrBear)",
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
