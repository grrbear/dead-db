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
        out.add(name.replace("'", "’"))
        out.add(name.replace("'", ""))
    if "’" in name:
        out.add(name.replace("’", "'"))
        out.add(name.replace("’", ""))
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

    # aliases (setdefault: canonical takes priority over alias for same surface)
    for alias_lower, canonical in aliases.items():
        for v in _apostrophe_variants(alias_lower):
            table.setdefault(v, (canonical, True))

    # build one combined regex with word boundaries
    # sort by length desc so "Scarlet Begonias" matches before "Scarlet"
    surfaces = sorted(table.keys(), key=len, reverse=True)
    pattern = (r"(?<!\w)("
               + "|".join(re.escape(s) for s in surfaces)
               + r")(?!\w)")
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
        quoted = (start >= 1 and text[start - 1] in "\"'“‘"
                  and end < len(text) and text[end] in "\"'”’")

        # segue notation immediately preceding?
        pre = text[max(0, start - 8):start]
        preceded_by_segue = bool(SEGUE_RE.search(pre))

        # title case: every alphabetic word must start with uppercase
        title_case = all(
            w[0].isupper() for w in surface.split() if w and w[0].isalpha()
        )

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
