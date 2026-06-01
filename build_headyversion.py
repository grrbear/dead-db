"""Build the community_votes table in dead.db from HeadyVersion.

Runnable:
  python3 -m build_headyversion               # full build
  python3 -m build_headyversion --propose-aliases   # generate alias proposals (no DB write)
  python3 -m build_headyversion --retry-songs       # re-scrape songs in low_yield log

Idempotent. Creates table if absent. Upserts on submission_id.
Does NOT touch build_db.py's tables.
"""
import difflib
import html
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from lore.fetchers.headyversion import (
    SongLink,
    discover_songs,
    fetch_song_submissions,
    iter_submissions,
)
from lore.song_matcher import _load_aliases

DEAD_DB_PATH = os.environ.get("DEAD_DB_PATH", "/hddpool/datastore/dead.db")
LORE_DIR = Path(__file__).parent / "lore"

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
    heady_url        TEXT NOT NULL,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cv_song_uuid    ON community_votes(song_uuid);
CREATE INDEX IF NOT EXISTS idx_cv_show_date    ON community_votes(show_date);
CREATE INDEX IF NOT EXISTS idx_cv_vote_score   ON community_votes(vote_score DESC);
CREATE INDEX IF NOT EXISTS idx_cv_heady_song   ON community_votes(heady_song_id);
"""


_SEGUE_SPLIT_RE = re.compile(r'\s*(?:->|>)\s*')


def _is_medley(name: str) -> bool:
    # Medley entries use ->, >, or & as separators.
    return "->" in name or ">" in name or "&" in name


def _first_song_from_segue(name: str) -> str:
    """Extract the first song from 'A -> B > C'."""
    return _SEGUE_SPLIT_RE.split(name, 1)[0].strip()


def _build_name_to_uuid_map(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, uuid in conn.execute("SELECT name, uuid FROM songs"):
        out[name.lower()] = uuid
    return out


def _resolve_song_uuid(
    hv_name: str,
    name_map: dict[str, str],
    alias_map: dict[str, str],
) -> str | None:
    """Try exact lowercase, apostrophe-fold, then alias lookup.
    Medley names are NOT passed here — caller screens with _is_medley() first.
    """
    if not hv_name:
        return None
    key = hv_name.lower()
    # pass 1: exact lowercase
    if key in name_map:
        return name_map[key]
    # pass 2: apostrophe-fold
    folded = key.replace("’", "'").replace("'", "")
    for n_lower, uuid in name_map.items():
        if n_lower.replace("’", "'").replace("'", "") == folded:
            return uuid
    # pass 3: alias map (song_aliases.txt)
    canonical = alias_map.get(key)
    if canonical and canonical.lower() in name_map:
        return name_map[canonical.lower()]
    return None


def _upsert_submission(cur: sqlite3.Cursor, s, uuid: str | None, now: str) -> None:
    cur.execute("""
        INSERT INTO community_votes(
            submission_id, heady_song_id, song_uuid, song_name,
            show_date, venue, city, vote_score, blurb,
            heady_url, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(submission_id) DO UPDATE SET
            heady_song_id=excluded.heady_song_id,
            song_uuid=excluded.song_uuid,
            song_name=excluded.song_name,
            show_date=excluded.show_date,
            venue=excluded.venue,
            city=excluded.city,
            vote_score=excluded.vote_score,
            blurb=excluded.blurb,
            heady_url=excluded.heady_url,
            fetched_at=excluded.fetched_at
    """, (
        s.submission_id, s.heady_song_id, uuid, s.song_name,
        s.show_date, s.venue, s.city, s.vote_score, s.blurb,
        s.submission_url, now,
    ))


# ---------- segue resolution ----------

def resolve_segues(
    name_map: dict[str, str],
    alias_map: dict[str, str],
) -> int:
    """Set song_uuid on segue rows by mapping to the first song in the segue.

    Runs after the main upsert loop so all rows exist before we try to resolve them.
    Returns the number of rows updated.
    """
    conn = sqlite3.connect(DEAD_DB_PATH)
    rows = conn.execute("""
        SELECT submission_id, song_name FROM community_votes
        WHERE song_uuid IS NULL
          AND (song_name LIKE '%->%' OR song_name LIKE '%>%')
    """).fetchall()

    n_resolved = 0
    for sub_id, song_name in rows:
        first = _first_song_from_segue(html.unescape(song_name))
        uuid = _resolve_song_uuid(first, name_map, alias_map)
        if uuid:
            conn.execute(
                "UPDATE community_votes SET song_uuid = ? WHERE submission_id = ?",
                (uuid, sub_id),
            )
            n_resolved += 1

    conn.commit()
    conn.close()
    print(f"[hv] segue resolution: {n_resolved}/{len(rows)} rows mapped to first-song UUID")
    return n_resolved


# ---------- main build ----------

def main(songs: list[SongLink] | None = None) -> int:
    """Full build (or partial re-run if songs is provided)."""
    conn = sqlite3.connect(DEAD_DB_PATH)
    conn.executescript(SCHEMA)
    name_map = _build_name_to_uuid_map(conn)
    alias_map = _load_aliases()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if songs is None:
        songs = discover_songs()
        print(f"[hv] discovered {len(songs)} songs from index")

    unresolved_names: dict[str, int] = {}
    medley_names: dict[str, int] = {}
    actual_counts: dict[int, int] = {}   # heady_song_id -> submission count
    n_subs = n_resolved = 0

    cur = conn.cursor()
    for sl in songs:
        subs = fetch_song_submissions(sl.url)
        actual_counts[sl.heady_song_id] = len(subs)
        for s in subs:
            decoded_name = html.unescape(s.song_name)
            if _is_medley(decoded_name):
                medley_names[decoded_name] = medley_names.get(decoded_name, 0) + 1
                uuid = None
            else:
                uuid = _resolve_song_uuid(decoded_name, name_map, alias_map)
                if uuid:
                    n_resolved += 1
                else:
                    unresolved_names[decoded_name] = unresolved_names.get(decoded_name, 0) + 1
            _upsert_submission(cur, s, uuid, now)
            n_subs += 1
        if n_subs % 200 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    # --- medleys log ---
    if medley_names:
        log = LORE_DIR / "headyversion_medleys_skipped.log"
        lines = [f"{n}\t{name}" for name, n in
                 sorted(medley_names.items(), key=lambda kv: -kv[1])]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[hv] {len(medley_names)} medley names ({sum(medley_names.values())} rows) "
              f"skipped resolution -> {log.name}")

    # --- unresolved log ---
    if unresolved_names:
        log = LORE_DIR / "headyversion_unresolved_songs.log"
        lines = [f"{n}\t{name}" for name, n in
                 sorted(unresolved_names.items(), key=lambda kv: -kv[1])]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[hv] {len(unresolved_names)} unresolved song names "
              f"({sum(unresolved_names.values())} rows) -> {log.name}")

    # --- low-yield log ---
    low_yield: list[SongLink] = []
    for sl in songs:
        actual = actual_counts.get(sl.heady_song_id, 0)
        if sl.expected_count > 0 and actual < sl.expected_count * 0.5:
            low_yield.append(sl)
    if low_yield:
        log = LORE_DIR / "headyversion_low_yield_songs.log"
        lines = [
            f"{sl.heady_song_id}\t{sl.slug}\t{sl.url}\t"
            f"{sl.expected_count}\t{actual_counts.get(sl.heady_song_id, 0)}"
            for sl in low_yield
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[hv] {len(low_yield)} low-yield songs (<50% of expected) -> {log.name}")

    print(f"[hv] wrote {n_subs} submissions, resolved {n_resolved} to song_uuid")

    # segue pass: map first-song of segue entries to their UUID
    resolve_segues(name_map, alias_map)

    # validation floor
    assert n_subs >= 5000, f"expected >=5000 submissions, got {n_subs}"
    assert n_resolved >= int(n_subs * 0.8), (
        f"only {n_resolved}/{n_subs} resolved to song_uuid; "
        "song name mapping is broken"
    )
    return 0


# ---------- --propose-aliases ----------

def propose_aliases() -> int:
    """Generate lore/headyversion_alias_proposals.txt from existing community_votes.

    Reads current community_votes, collects song_names with NULL song_uuid
    that are not medleys, runs difflib.get_close_matches against the canon,
    writes a proposal file for manual review. Does NOT write to song_aliases.txt.
    """
    conn = sqlite3.connect(f"file:{DEAD_DB_PATH}?mode=ro", uri=True)
    name_map = _build_name_to_uuid_map(conn)
    alias_map = _load_aliases()

    # collect unresolved counts from the live table
    unresolved: dict[str, int] = {}
    for (song_name, cnt) in conn.execute("""
        SELECT song_name, COUNT(*) FROM community_votes
        WHERE song_uuid IS NULL GROUP BY song_name
    """):
        decoded = html.unescape(song_name)
        if not _is_medley(decoded):
            unresolved[decoded] = int(cnt)
    conn.close()

    canon_names = list(name_map.keys())  # already lowercased

    out_lines: list[str] = [
        "# HeadyVersion alias proposals — review carefully before adding to song_aliases.txt",
        "# Format: hv name = canonical name",
        "# Lines starting with # are comments.",
        "",
    ]
    n_proposed = 0
    for hv_name, count in sorted(unresolved.items(), key=lambda kv: -kv[1]):
        # skip if it already resolves (race: may have been fixed since table was built)
        if _resolve_song_uuid(hv_name, name_map, alias_map):
            continue
        candidates = difflib.get_close_matches(hv_name.lower(), canon_names, n=3, cutoff=0.6)
        if not candidates:
            continue
        ratios = [(c, difflib.SequenceMatcher(None, hv_name.lower(), c).ratio()) for c in candidates]
        suggest_str = ", ".join(f'"{c}" ({r:.2f})' for c, r in ratios)
        out_lines.append(f"# {count} rows — HV: \"{hv_name}\"")
        out_lines.append(f"#   suggest: {suggest_str}")
        # pre-fill with the best match for easy editing
        best_canonical = ratios[0][0]
        # look up the original-case canonical name
        for db_name, _ in name_map.items():
            if db_name == best_canonical:
                best_canonical = db_name
                break
        out_lines.append(f"{hv_name} = {best_canonical}")
        out_lines.append("")
        n_proposed += 1

    proposals_file = LORE_DIR / "headyversion_alias_proposals.txt"
    proposals_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"[hv] wrote {n_proposed} alias proposals -> {proposals_file}")
    print("Review and edit the file, then copy approved lines into lore/song_aliases.txt")
    return 0


# ---------- --retry-songs ----------

def retry_songs() -> int:
    """Re-scrape songs listed in lore/headyversion_low_yield_songs.log."""
    log = LORE_DIR / "headyversion_low_yield_songs.log"
    if not log.exists():
        print("[hv] no low_yield log found; nothing to retry")
        return 0
    songs: list[SongLink] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sid, slug, url, expected_s, _ = parts[0], parts[1], parts[2], parts[3], parts[4]
        songs.append(SongLink(
            heady_song_id=int(sid),
            slug=slug,
            url=url,
            expected_count=int(expected_s),
        ))
    if not songs:
        print("[hv] low_yield log is empty; nothing to retry")
        return 0
    print(f"[hv] retrying {len(songs)} low-yield songs")
    return main(songs=songs)


# ---------- entry point ----------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--propose-aliases":
        sys.exit(propose_aliases())
    elif mode == "--retry-songs":
        sys.exit(retry_songs())
    elif mode == "" or mode == "--full":
        sys.exit(main())
    else:
        print(f"Unknown mode: {mode!r}", file=sys.stderr)
        print("Usage: python3 -m build_headyversion [--propose-aliases | --retry-songs]", file=sys.stderr)
        sys.exit(1)
