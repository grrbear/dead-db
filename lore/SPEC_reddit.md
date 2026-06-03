# dead-db — Reddit fetcher (corpus #6, Arctic Shift dumps)

Source: r/gratefuldead, ingested from Arctic Shift per-subreddit dumps
(.zst NDJSON), read OFFLINE. No network, no auth, no Reddit API — the API
is approval-gated (RBP) and unauth .json now 403s. Dumps are Arctic Shift's
sanctioned path for whole-subreddit data and give complete history.

Locked decisions:
- source="reddit"; one document per submission; source_id = permalink.
- Two dump files live in lore/data/reddit/ (posts + comments, .jsonl NDJSON),
  downloaded manually by the user. Fetcher locates them by glob.
- No new dependencies. stdlib only. No praw/requests/curl-cffi/zstandard.
- Quality gates (env-tunable), identical to the parked reddit_api.py:
  post: score>=10 AND (len(selftext)>=200 OR >=3 qualifying comments); skip
  stickied/over_18. comment: score>=5 AND len>=120 AND not deleted/removed
  AND author not AutoModerator; keep top 40 by score, flattened.
- Slash-date annotation unchanged: "5/8/77" -> "5/8/77 [1977-05-08]" inline
  so normalize.py's ISO DATE_RE populates mentioned_dates.
- published = post created_utc (NOT the show date discussed).
- Router Option B weighting (PER_SOURCE_CAP["reddit"]=4 + SOURCE_WEIGHT
  reddit:0.85 re-rank) is ALREADY in router.py — leave it.
- Out of scope: the live API path (parked as reddit_api.py), more subreddits,
  incremental/delta ingest, comment threading (flattened by score).
