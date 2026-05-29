# dead-db phase 3 — song-name matching (enrichment)

## Context

Fifth and final ingest-side piece of phase 3. **No new corpus.** This phase
populates the `chunks.mentioned_songs` field across the four existing
corpora (Wikipedia, LIA essays, LIA sources, Books — Deadcast deferred).
Until now `mentioned_songs` has been `"[]"` everywhere, by design.

Goal: when a chunk discusses one or more Grateful Dead songs by name, the
chunk gets tagged with the canonical song name(s) from `dead.db.songs.name`,
plus the surface form found and a confidence score. This is the bridge that
makes "what does the lore say about Scarlet > Fire" actually find the right
chunks even when the prose says just "Scarlet" or "Scarlet into Fire."

Read `lore/SPEC.md`, `lore/SPEC_wikipedia.md`, `lore/SPEC_lia.md`, and
`lore/SPEC_books.md` before this file. This spec assumes all four are
implemented and committed.

---

## Locked decisions carried from earlier phases (do not relitigate)

sqlite-vec; bge-small-en-v1.5 @ 384 dim CPU; separate dead_lore.db; raw_text
stored in documents; section hints via RawDocument.sections; chunks.section
per-chunk heading; documents.metadata JSON for per-doc per-corpus metadata.
stdlib preferred (urllib, no requests). See prior specs.

## New decisions for this phase

- **Two-mode delivery.** This phase ships song matching as a SEPARATE
  POST-PASS over the existing dead_lore.db FIRST. Once the matcher is
  validated against real chunks, the same `match_songs(text) -> list[Match]`
  function is wired into `normalize.py` so future ingests get tagging at
  write time. Both modes call the SAME matcher function — no logic
  duplication.
- **Three-layer matcher, precision over recall.**
  1. Canonical + mechanical-variant gazetteer built from `dead.db.songs.name`
     (apostrophe variants, "The"-prefix variants, case-insensitive base form).
  2. Hand-curated abbreviation gazetteer (`lore/song_aliases.txt`) for the
     famously-abbreviated Dead canon ("Scarlet" -> "Scarlet Begonias",
     "Help/Slip/Franklin's" -> three songs at once, etc.). Editable file,
     user curates over time.
  3. Stopword gating + context boost: a curated list of song titles that
     ALSO appear as common English phrases requires stronger evidence to
     match (title-case only, or context-marker nearby). Context markers
     ("played", "opened with", "into", ">", "version of", etc.) boost
     confidence on any match.
- **Rich tagging schema.** `chunks.mentioned_songs` stores a JSON list of
  `{"name": "<canonical>", "confidence": 0.0-1.0, "surface_form": "<as
  found>"}` dicts, NOT a flat list of names. Surface form is the literal
  substring found in the chunk so a curation script can audit matches.
  No new schema column needed — the existing TEXT field just stores richer
  JSON.
- **Read-only dependency on `dead.db`.** The matcher reads canonical song
  names from `/hddpool/datastore/dead.db` (the existing setlist DB) via a
  read-only sqlite3 connection. Does NOT write to dead.db. Does NOT join
  the two DBs at query time — it pulls the song list once at matcher init
  and caches it.
- **Confidence is calibrated, not magic.** Spec defines exact confidence
  thresholds. Default 1.0 baseline minus penalties for risk factors plus
  bonuses for context. Documented and editable — not a black box.
- **Idempotent re-runnable post-pass.** `python3 -m lore.match_songs` can
  be re-run any time after corpus rebuilds, or after editing the aliases /
  stopwords files. It overwrites `mentioned_songs` for every chunk;
  doesn't append.

---

## Confidence model (spec, not implementation)

Every candidate match starts at base confidence 1.0 and is adjusted:

**Penalties (multiplicative):**
- Surface form is in the stopword list AND was found in lowercase: ×0.0
  (reject outright; lowercase "the wheel" is not the song).
- Surface form is in the stopword list AND was found in proper case but no
  context marker nearby: ×0.4 (kept but low confidence — let router decide).
- Surface form came from the alias gazetteer (an abbreviation, not the
  canonical name): ×0.85 (mechanical hit, still strong, but slightly less
  certain than a canonical full-name hit).
- Surface form matches a song name that is ≤ 5 characters AND not in
  quotation marks AND no context marker nearby: ×0.5 (very short titles
  like "Fire", "Wharf", "Eyes" are too easy to false-positive without
  context).

**Bonuses (additive, capped at 1.0):**
- Context marker within ±60 chars (see CONTEXT_MARKERS below): +0.15
- Surface form is inside quotation marks (straight or curly): +0.20
- Surface form is preceded by `>`, `→`, `->`, `/`, or `into` (segue
  notation): +0.25
- Another already-matched song appears within ±100 chars of this match
  (co-occurrence): +0.10

**Final filter:**
- Matches with confidence < 0.3 are dropped before write.
- Within a chunk, if the SAME canonical song is matched multiple times,
  keep only the highest-confidence instance (deduplicate by `name`).

The thresholds and weights are constants at the top of the matcher module —
NOT hardcoded inline. Easy to tune.

---

## File layout

```
dead-db/
  lore/
    song_aliases.txt          # NEW — hand-curated abbreviation map, seeded
    song_stopwords.txt        # NEW — hand-curated common-phrase title list
    song_matcher.py           # NEW — match_songs() + gazetteer build
    match_songs.py            # NEW — post-pass runner over dead_lore.db
    normalize.py              # MODIFIED — call match_songs() at chunk time
    fetchers/_base.py         # unchanged
```

---

## `lore/song_aliases.txt` — seeded abbreviation map

Format: `alias = Canonical Song Name`. One per line. `#` starts a comment.
Aliases match case-insensitively at lookup; canonical name must EXACTLY
match a `dead.db.songs.name` row (verified at gazetteer build — mismatches
fail loudly with the bad rows printed).

```
# Hand-curated abbreviations for famously-shortened Dead canon.
# alias = Canonical Song Name (must match dead.db.songs.name exactly)
# Edit and re-run match_songs.py — matcher is idempotent.

# === The pair / triple segues ===
Scarlet > Fire = Scarlet Begonias
Scarlet/Fire = Scarlet Begonias
Scarlet into Fire = Scarlet Begonias
Help > Slip > Franklin's = Help on the Way
Help/Slip/Franklin's = Help on the Way
Slipknot! = Slipknot!
Franklin's Tower = Franklin's Tower
China > Rider = China Cat Sunflower
China Cat > Rider = China Cat Sunflower
Lazy Lightning > Supplication = Lazy Lightning
Supplication = Supplication
Estimated > Eyes = Estimated Prophet
Estimated > He's Gone = Estimated Prophet

# === Standalone common abbreviations ===
Scarlet = Scarlet Begonias
Fire on the Mountain = Fire on the Mountain
Fire = Fire on the Mountain
Dark Star = Dark Star
The Other One = The Other One
Other One = The Other One
St. Stephen = St. Stephen
Saint Stephen = St. Stephen
St Stephen = St. Stephen
Eyes of the World = Eyes of the World
Eyes = Eyes of the World
Playin' = Playing in the Band
Playing = Playing in the Band
Help on the Way = Help on the Way
Franklin's = Franklin's Tower
Truckin' = Truckin'
Truckin = Truckin'
Sugar Mag = Sugar Magnolia
Sugaree = Sugaree
Bertha = Bertha
Loser = Loser
Jack Straw = Jack Straw
Ramble On Rose = Ramble On Rose
Tennessee Jed = Tennessee Jed
Mexicali = Mexicali Blues
Cumberland = Cumberland Blues
Casey Jones = Casey Jones
Uncle John's = Uncle John's Band
UJB = Uncle John's Band
Ripple = Ripple
Box of Rain = Box of Rain
Friend of the Devil = Friend of the Devil
FOTD = Friend of the Devil
Brokedown = Brokedown Palace
Ship of Fools = Ship of Fools
Stella Blue = Stella Blue
Wharf Rat = Wharf Rat
Morning Dew = Morning Dew
Black Peter = Black Peter
Comes a Time = Comes a Time
Bird Song = Bird Song
Cassidy = Cassidy
Estimated = Estimated Prophet
Terrapin = Terrapin Station
Iko = Iko Iko
GDTRFB = Going Down the Road Feeling Bad
Goin' Down the Road = Going Down the Road Feeling Bad

# === Pigpen-era ===
Lovelight = Turn On Your Lovelight
Good Lovin' = Good Lovin'
NFA = Not Fade Away
Not Fade Away = Not Fade Away
Caution = Caution (Do Not Stop on Tracks)
Midnight Hour = In the Midnight Hour

# === Later era ===
Touch of Grey = Touch of Grey
Touch of Gray = Touch of Grey
Throwing Stones = Throwing Stones
Hell in a Bucket = Hell in a Bucket
West LA = West L.A. Fadeaway
Standing on the Moon = Standing on the Moon
Black Muddy River = Black Muddy River
Built to Last = Built to Last
Foolish Heart = Foolish Heart

# === Covers played a lot ===
Big River = Big River
Me and My Uncle = Me and My Uncle
El Paso = El Paso
Mama Tried = Mama Tried
Mr. Charlie = Mr. Charlie
Cold Rain and Snow = Cold Rain and Snow
Iko Iko = Iko Iko
```

Implementer note: gazetteer build MUST verify every right-hand-side value
appears in `dead.db.songs.name`. If a canonical name is misspelled here, the
build fails with a clear "alias <X> points to non-existent canonical <Y>"
error listing all bad rows. Do NOT auto-correct; that's the user's job.

---

## `lore/song_stopwords.txt` — seeded common-phrase title list

Format: one canonical song name per line, exact match against `dead.db.songs.name`.
`#` starts a comment. These titles are also common English phrases, so a
match requires either (a) title-case form OR (b) a context marker nearby.
Lowercase mentions are rejected outright.

```
# Songs whose titles are also common English phrases.
# Require title-case OR context marker. Lowercase form rejects outright.
# Edit and re-run match_songs.py.

The Wheel
Truckin'
Estimated Prophet
Throwing Stones
He's Gone
Looks Like Rain
Friend of the Devil
Box of Rain
Brokedown Palace
Black Peter
Big River
Big Boss Man
Comes a Time
Cold Rain and Snow
Mama Tried
Me and My Uncle
Mr. Charlie
Touch of Grey
Hell in a Bucket
Standing on the Moon
Black Muddy River
Built to Last
Foolish Heart
Not Fade Away
Going Down the Road Feeling Bad
Around and Around
Promised Land
The Promised Land
Good Lovin'
Loose Lucy
Easy Wind
New Speedway Boogie
Operator
Tomorrow Is Forever
```

---

## `lore/song_matcher.py` — the matcher

```python
"""Song-name matcher: text -> list[Match] over the dead.db.songs canon.
Three-layer: canonical + variants, hand-curated aliases, stopword/context.
stdlib only; reads dead.db read-only at init.
"""
import os
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEAD_DB_PATH = os.environ.get("DEAD_DB_PATH", "/hddpool/datastore/dead.db")
ALIASES_FILE = Path(__file__).parent / "song_aliases.txt"
STOPWORDS_FILE = Path(__file__).parent / "song_stopwords.txt"

# context markers that boost confidence when found within CONTEXT_WINDOW
# characters of a match
CONTEXT_WINDOW = 60
CONTEXT_MARKERS = (
    r"\bplayed\b", r"\bplaying\b", r"\bopened with\b", r"\bclosed with\b",
    r"\bencored?\b", r"\bjam(?:med|ming)?\b", r"\bversion(?:s)? of\b",
    r"\binto\b", r"\bsegue(?:d)?\b", r">", r"→", r"->", r"/",
    r"\bperformed\b", r"\bsang\b", r"\bsings\b", r"\bcovered\b",
    r"\btune\b", r"\bsong\b", r"\btrack\b",
)
_CTX_RE = re.compile("|".join(CONTEXT_MARKERS), re.IGNORECASE)

# segue notation that strongly implies song context
SEGUE_RE = re.compile(r"(?:^|\s)(?:>|→|->|/|into)\s*$", re.IGNORECASE)

# confidence model constants
BASE_CONFIDENCE = 1.0
PENALTY_STOPWORD_LOWERCASE = 0.0          # outright reject
PENALTY_STOPWORD_NO_CONTEXT = 0.4
PENALTY_ALIAS_HIT = 0.85
PENALTY_SHORT_NO_CONTEXT = 0.5
BONUS_CONTEXT_MARKER = 0.15
BONUS_QUOTED = 0.20
BONUS_SEGUE_PRECEDES = 0.25
BONUS_COOCCURRENCE = 0.10
SHORT_NAME_LEN = 5
COOCCURRENCE_WINDOW = 100
MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class Match:
    name: str            # canonical song name
    confidence: float
    surface_form: str    # literal substring found in the text
    start: int           # char offset in text


def _load_canon(db_path: str = DEAD_DB_PATH) -> list[str]:
    """Read song names from dead.db, read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM songs").fetchall()]
    finally:
        conn.close()


def _load_aliases() -> dict[str, str]:
    """alias (lowercased) -> canonical name (exact)."""
    out: dict[str, str] = {}
    if not ALIASES_FILE.exists():
        return out
    for line in ALIASES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        alias, canonical = (s.strip() for s in line.split("=", 1))
        if alias and canonical:
            out[alias.lower()] = canonical
    return out


def _load_stopwords() -> set[str]:
    if not STOPWORDS_FILE.exists():
        return set()
    out: set[str] = set()
    for line in STOPWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _apostrophe_variants(name: str) -> set[str]:
    """Generate apostrophe-fold variants of a name."""
    out = {name}
    if "'" in name:
        out.add(name.replace("'", "'"))
        out.add(name.replace("'", ""))
    if "'" in name:
        out.add(name.replace("'", "'"))
        out.add(name.replace("'", ""))
    return out


@lru_cache(maxsize=1)
def _gazetteer() -> tuple[dict, set[str], re.Pattern]:
    """Returns (surface_lower -> (canonical, is_alias), stopwords, combined_regex).

    The regex finds any surface form in a text in one pass; the lookup table
    resolves a hit to its canonical name and tells us if it came from the
    alias gazetteer (penalty applies later).
    """
    canon = _load_canon()
    aliases = _load_aliases()
    stopwords = _load_stopwords()

    # validate aliases point at real canonical names — fail loudly
    canon_set = set(canon)
    bad = sorted({c for c in aliases.values() if c not in canon_set})
    if bad:
        raise RuntimeError(
            "song_aliases.txt contains aliases pointing to non-existent canonical "
            f"songs (not in dead.db.songs.name): {bad}"
        )

    # validate stopwords are real song names too
    bad_stop = sorted({s for s in stopwords if s not in canon_set})
    if bad_stop:
        raise RuntimeError(
            f"song_stopwords.txt contains non-existent canonical songs: {bad_stop}"
        )

    table: dict[str, tuple[str, bool]] = {}   # surface_lower -> (canonical, is_alias)

    # canonical names + apostrophe variants (not aliases)
    for name in canon:
        for v in _apostrophe_variants(name):
            table.setdefault(v.lower(), (name, False))

    # aliases (override only if not already a canonical surface)
    for alias_lower, canonical in aliases.items():
        for v in _apostrophe_variants(alias_lower):
            table.setdefault(v, (canonical, True))

    # build one combined regex with word boundaries
    # sort by length desc so "Scarlet Begonias" matches before "Scarlet"
    surfaces = sorted(table.keys(), key=len, reverse=True)
    pattern = r"(?:(?<=\W)|(?<=^))(" + "|".join(re.escape(s) for s in surfaces) + r")(?=\W|$)"
    combined = re.compile(pattern, re.IGNORECASE)

    return table, stopwords, combined


def match_songs(text: str) -> list[Match]:
    """Return high-confidence song matches in `text`. Idempotent / stateless."""
    table, stopwords, regex = _gazetteer()
    if not text:
        return []

    raw_hits: list[Match] = []

    for m in regex.finditer(text):
        surface = m.group(1)
        surface_lower = surface.lower()
        if surface_lower not in table:
            continue
        canonical, is_alias = table[surface_lower]
        start, end = m.start(1), m.end(1)
        # context window
        ctx_start = max(0, start - CONTEXT_WINDOW)
        ctx_end = min(len(text), end + CONTEXT_WINDOW)
        ctx = text[ctx_start:ctx_end]
        has_context = bool(_CTX_RE.search(ctx))

        # quoted? check 2 chars before and after for quote characters
        quoted = (start >= 1 and text[start - 1] in "\"'\u201c\u2018"
                  and end < len(text) and text[end] in "\"'\u201d\u2019")

        # segue notation immediately preceding?
        pre = text[max(0, start - 8):start]
        preceded_by_segue = bool(SEGUE_RE.search(pre))

        # is this in proper title case (heuristic: surface starts with uppercase)
        title_case = surface[0].isupper()

        conf = BASE_CONFIDENCE

        # stopword gating
        if canonical in stopwords:
            if not title_case:
                continue       # reject outright
            if not has_context:
                conf *= PENALTY_STOPWORD_NO_CONTEXT

        # alias penalty
        if is_alias:
            conf *= PENALTY_ALIAS_HIT

        # short-name risk
        if len(surface) <= SHORT_NAME_LEN and not has_context and not quoted:
            conf *= PENALTY_SHORT_NO_CONTEXT

        # bonuses (additive, capped)
        if has_context:
            conf = min(1.0, conf + BONUS_CONTEXT_MARKER)
        if quoted:
            conf = min(1.0, conf + BONUS_QUOTED)
        if preceded_by_segue:
            conf = min(1.0, conf + BONUS_SEGUE_PRECEDES)

        raw_hits.append(Match(canonical, conf, surface, start))

    # co-occurrence bonus: any hit within COOCCURRENCE_WINDOW of another hit
    boosted: list[Match] = []
    for i, h in enumerate(raw_hits):
        bonus = 0.0
        for j, other in enumerate(raw_hits):
            if i == j or other.name == h.name:
                continue
            if abs(other.start - h.start) <= COOCCURRENCE_WINDOW:
                bonus = BONUS_COOCCURRENCE
                break
        boosted.append(Match(h.name, min(1.0, h.confidence + bonus),
                             h.surface_form, h.start))

    # filter by min confidence + dedupe by canonical name (keep highest conf)
    by_name: dict[str, Match] = {}
    for h in boosted:
        if h.confidence < MIN_CONFIDENCE:
            continue
        cur = by_name.get(h.name)
        if cur is None or h.confidence > cur.confidence:
            by_name[h.name] = h

    return sorted(by_name.values(), key=lambda m: (-m.confidence, m.name))


def to_json_list(matches: list[Match]) -> list[dict]:
    """Project Match -> the dict shape stored in chunks.mentioned_songs."""
    return [
        {"name": m.name, "confidence": round(m.confidence, 3),
         "surface_form": m.surface_form}
        for m in matches
    ]
```

---

## `lore/match_songs.py` — the post-pass runner

```python
"""Post-pass: walk every chunk in dead_lore.db, run match_songs(), overwrite
chunks.mentioned_songs. Idempotent. Run: python3 -m lore.match_songs
"""
import json
import sqlite3
from collections import Counter

from .config import LORE_DB_PATH
from .db import connect
from .song_matcher import match_songs, to_json_list


def main() -> int:
    conn = connect(LORE_DB_PATH)
    cur = conn.cursor()
    n_chunks = cur.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"[match_songs] scanning {n_chunks} chunks")

    stats: Counter = Counter()
    name_counts: Counter = Counter()
    BATCH = 500
    offset = 0
    while True:
        rows = cur.execute(
            "SELECT id, text FROM chunks ORDER BY id LIMIT ? OFFSET ?",
            (BATCH, offset)
        ).fetchall()
        if not rows:
            break
        updates = []
        for chunk_id, text in rows:
            matches = match_songs(text)
            updates.append((json.dumps(to_json_list(matches)), chunk_id))
            stats["chunks_scanned"] += 1
            if matches:
                stats["chunks_with_match"] += 1
                stats["total_matches"] += len(matches)
                for m in matches:
                    name_counts[m.name] += 1
        cur.executemany(
            "UPDATE chunks SET mentioned_songs = ? WHERE id = ?",
            updates
        )
        offset += BATCH
        conn.commit()

    conn.close()

    print(f"[match_songs] scanned: {stats['chunks_scanned']}")
    print(f"[match_songs] chunks with >=1 match: {stats['chunks_with_match']}")
    print(f"[match_songs] total matches: {stats['total_matches']}")
    print(f"[match_songs] top 25 songs by chunk count:")
    for name, n in name_counts.most_common(25):
        print(f"  {n:5d}  {name}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

---

## `lore/normalize.py` — wiring into ingest

After the post-pass is validated, modify `normalize.py` so future ingests
tag songs at chunk creation time. Single change point: in `normalize()`
where chunks are flushed, replace `mentioned_songs=[]` with:

```python
from .song_matcher import match_songs, to_json_list
# inside flush(), where the Chunk dataclass is built:
mentioned_songs=to_json_list(match_songs(text)),
```

The `Chunk.mentioned_songs` field stays typed `list` (now of dicts instead
of strings). `build_lore_db.py`'s INSERT already `json.dumps()` the field,
so no change there.

Verify the existing smoke test still passes — the FakeFetcher chunks don't
mention specific Dead canon, so `mentioned_songs` will be `[]` for them.
The smoke test's assertions don't inspect mentioned_songs, so they still pass.

---

## Success criteria

1. New files exist: `lore/song_aliases.txt`, `lore/song_stopwords.txt`,
   `lore/song_matcher.py`, `lore/match_songs.py`.
2. `normalize.py` modified to call `match_songs()` (the wiring step).
3. Smoke test still passes unchanged
   (`python3 -m lore.smoke_test` -> "OK: 3 docs, 3 chunks...").
4. **Validation step BEFORE the full run.** Hand-test the matcher on three
   strings and print the result. Show me the output:
   ```python
   from lore.song_matcher import match_songs, to_json_list
   to_json_list(match_songs(
       'On 1977-05-08 the band opened the second set with Scarlet > Fire, '
       'one of the best versions ever played.'
   ))
   # expect: Scarlet Begonias (high), Fire on the Mountain (high)

   to_json_list(match_songs(
       'The wheel of fortune turned for the band that year.'
   ))
   # expect: [] (lowercase "the wheel" rejected by stopword gating)

   to_json_list(match_songs(
       'Estimated Prophet, with its 7/4 feel, was a Weir tune.'
   ))
   # expect: Estimated Prophet (high — title case + context markers)
   ```
5. Gazetteer build fails loudly if aliases or stopwords reference a song
   name not in `dead.db.songs.name`. Force a failure by adding a fake
   alias line `Fake Song = This Does Not Exist` to `song_aliases.txt`,
   confirm the error message lists `["This Does Not Exist"]`, then remove
   the test line.
6. `python3 -m lore.match_songs` runs over the full dead_lore.db. Show me:
   - chunks_scanned (sanity check vs total chunk count)
   - chunks_with_match (expect: a meaningful percentage — hard to predict,
     probably 15-40% of chunks across the corpora)
   - total_matches
   - top 25 songs by chunk count
   The top list is the real quality signal — if it's dominated by surprising
   songs or has obvious false positives (a song with hundreds more hits than
   plausible), the matcher needs tuning.
7. Spot-check three random chunks with matches in a REPL:
   ```python
   import sqlite3, json
   c = sqlite3.connect('/hddpool/datastore/dead_lore.db')
   for row in c.execute(
     "SELECT text, mentioned_songs FROM chunks "
     "WHERE mentioned_songs != '[]' AND mentioned_songs != '' "
     "ORDER BY random() LIMIT 3"
   ):
       print(row[1]); print(row[0][:300]); print("---")
   ```
   Each chunk's matches should be defensible against the surrounding text.
8. No git commit — user reviews diff and commits.

---

## Out of scope (do not build this phase)

- Lyric matching (chunks that quote lyrics but don't name the song). Hard
  problem; ignore.
- Show-date inference from song co-occurrence patterns. Cool idea, separate
  project.
- Fuzzy/typo-tolerant matching beyond apostrophe folding. The corpus is
  edited prose; typos are rare.
- Re-ingest of any corpus. The post-pass updates in place; no rebuild
  needed. Future ingests get matching automatically via normalize.py.
- Tuning the confidence weights from data. Ship with the defaults; tune
  after you see real top-25 output and audit a few suspect chunks.
- MCP tools (dead_lore, dead_ask) — the final phase, comes next.

---

## Notes for the implementer

- Read this file + lore/SPEC.md before coding.
- The matcher is the heart of this; the runner is a thin wrapper. Get
  match_songs() right and the rest is trivial. Spend the validation time on
  step 4 of success criteria.
- Confidence thresholds are intentionally conservative. False positives are
  worse than false negatives here — a misleading mentioned_songs tag will
  silently corrupt retrieval relevance; a missing tag just means the
  vector search has to carry more weight on that chunk. Bias toward
  precision.
- The aliases and stopwords files are the user's curation surface. Do NOT
  hardcode aliases or stopwords inside song_matcher.py. The validation step
  (assert that every alias/stopword name exists in dead.db.songs.name) is
  the only file-content check the code does.
- match_songs() uses @lru_cache on the gazetteer build so init cost is paid
  once per process. The post-pass and normalize.py both benefit.
- stdlib only — sqlite3, re, json, pathlib, functools, dataclasses,
  collections. No new dependencies in requirements.txt.
- Match existing dead-db style: terse docstrings, stdlib over deps.
