"""r/gratefuldead fetcher — reads Arctic Shift per-subreddit dumps
(.jsonl NDJSON) offline. No network. Quality gates + slash-date annotation
identical to the parked reddit_api.py. stdlib only, no new deps."""
import os
import re
import json
import time
import glob
from typing import Iterator

from ._base import Fetcher, RawDocument

SUBREDDIT = "gratefuldead"
DATA_DIR = os.environ.get(
    "REDDIT_DUMP_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "reddit"),
)

MIN_POST_SCORE        = int(os.environ.get("REDDIT_MIN_POST_SCORE", "10"))
MIN_SELFTEXT          = int(os.environ.get("REDDIT_MIN_SELFTEXT", "200"))
MIN_GOOD_COMMENTS     = int(os.environ.get("REDDIT_MIN_GOOD_COMMENTS", "3"))
MIN_COMMENT_SCORE     = int(os.environ.get("REDDIT_MIN_COMMENT_SCORE", "5"))
MIN_COMMENT_LEN       = int(os.environ.get("REDDIT_MIN_COMMENT_LEN", "120"))
MAX_COMMENTS_PER_POST = int(os.environ.get("REDDIT_MAX_COMMENTS", "40"))
MAX_COMMENTS_COLLECT  = 200   # cap per-post collection, bounds memory

_SKIP_AUTHORS = {"AutoModerator", "[deleted]", None, ""}
_DEAD_BODIES  = {"[deleted]", "[removed]", ""}
LOOSE_DATE_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")


def _loose_to_iso(m):
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 1900 + y if y >= 60 else 2000 + y
    if not (1965 <= y <= 1995) or not (1 <= mo <= 12) or not (1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _annotate_dates(text):
    return LOOSE_DATE_RE.sub(
        lambda m: f"{m.group(0)} [{_loose_to_iso(m)}]" if _loose_to_iso(m) else m.group(0),
        text,
    )


def _stream_jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _find(kind):
    pats = [f"*{kind}*.jsonl", f"*{SUBREDDIT}*{kind}*.jsonl"]
    if kind == "posts":
        pats.append("*submission*.jsonl")
    hits = sorted({h for p in pats for h in glob.glob(os.path.join(DATA_DIR, p))})
    if not hits:
        raise FileNotFoundError(
            f"No {kind} .jsonl in {DATA_DIR}. Download the r/{SUBREDDIT} {kind} "
            f"dump from arctic-shift.photon-reddit.com/download-tool.")
    return hits[0]


class RedditFetcher(Fetcher):
    name = "reddit"

    def discover(self):
        return [SUBREDDIT]

    def fetch(self, source_ids=None) -> Iterator[RawDocument]:
        candidates = {}                       # pass 1: cheap post gate
        for p in _stream_jsonl(_find("posts")):
            if (p.get("subreddit", "").lower() == SUBREDDIT
                    and not p.get("stickied") and not p.get("over_18")
                    and (p.get("score") or 0) >= MIN_POST_SCORE):
                candidates[p["id"]] = p

        kept = {pid: [] for pid in candidates}  # pass 2: attach comments
        for c in _stream_jsonl(_find("comments")):
            link = (c.get("link_id") or "").removeprefix("t3_")
            lst = kept.get(link)
            if lst is None or len(lst) >= MAX_COMMENTS_COLLECT:
                continue
            body = (c.get("body") or "").strip()
            if ((c.get("score") or 0) >= MIN_COMMENT_SCORE
                    and len(body) >= MIN_COMMENT_LEN
                    and body not in _DEAD_BODIES
                    and c.get("author") not in _SKIP_AUTHORS):
                lst.append({"score": c["score"], "body": body})

        for pid, post in candidates.items():     # build documents
            comments = sorted(kept[pid], key=lambda x: x["score"], reverse=True)[:MAX_COMMENTS_PER_POST]
            selftext = (post.get("selftext") or "").strip()
            if len(selftext) < MIN_SELFTEXT and len(comments) < MIN_GOOD_COMMENTS:
                continue
            title = (post.get("title") or "").strip()
            parts = [title]
            if selftext:
                parts.append(selftext)
            if comments:
                parts.append("--- comments ---")
                parts.extend(f"[score {c['score']}] {c['body']}" for c in comments)
            raw_text = _annotate_dates("\n\n".join(parts))
            created = post.get("created_utc")
            published = time.strftime("%Y-%m-%d", time.gmtime(int(float(created)))) if created else None
            permalink = post.get("permalink") or f"/r/{SUBREDDIT}/comments/{pid}/"
            yield RawDocument(
                source="reddit", source_id=permalink,
                title=title or permalink,
                url=f"https://www.reddit.com{permalink}",
                published=published, raw_text=raw_text, sections=None,
                metadata={"subreddit": SUBREDDIT, "post_id": pid,
                          "score": post.get("score"),
                          "num_comments": post.get("num_comments"),
                          "flair": post.get("link_flair_text"),
                          "author": post.get("author"),
                          "n_comments_kept": len(comments)},
            )
