#!/usr/bin/env python3
"""Phase 4 gap-fill: build the archive_recordings table in dead.db.

Pipeline:
  1. scrape_archive.py  -> archive_raw.jsonl   (cursor scrape of GratefulDead collection)
  2. this script        -> archive_recordings table in data/dead.db

Idempotent: drops + rebuilds the table. Joins to `shows` on show_date (YYYY-MM-DD),
the same canonical key used by `plex_albums`. No change to existing tables.
"""
import json, re, sqlite3, sys, os

DB_PATH  = os.environ.get("DEAD_DB", "data/dead.db")
RAW_PATH = os.environ.get("ARCHIVE_RAW", "archive_raw.jsonl")

def classify_source(src, ident):
    t = (src or "").lower(); i = (ident or "").lower(); blob = t + " " + i
    if "matrix" in blob or ".mtx" in blob or re.search(r'\bmtx\b', blob): return "MATRIX"
    if re.search(r'\bfm\b', t) or ".fm." in i: return "FM"
    has_sbd = "sbd" in blob or "soundboard" in t or "board" in t
    has_aud = ("aud" in blob or "audience" in t or "microphone" in t
               or "mic" in t or "nak" in i)
    if has_sbd and not has_aud: return "SBD"
    if has_aud and not has_sbd: return "AUD"
    if has_sbd and has_aud: return "MATRIX"
    return "UNKNOWN"

def norm_date(d):
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', d or ""); return m.group(0) if m else None

def build(con):
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS archive_recordings")
    cur.execute("""CREATE TABLE archive_recordings(
        identifier  TEXT PRIMARY KEY,
        show_date   TEXT NOT NULL,
        source_type TEXT NOT NULL,
        venue       TEXT,
        coverage    TEXT,
        avg_rating  REAL,
        num_reviews INTEGER NOT NULL DEFAULT 0,
        downloads   INTEGER NOT NULL DEFAULT 0,
        archive_url TEXT NOT NULL)""")
    cur.execute("CREATE INDEX idx_archive_date ON archive_recordings(show_date)")
    n = skipped = 0
    for line in open(RAW_PATH):
        it = json.loads(line); date = norm_date(it.get("date"))
        if not date: skipped += 1; continue
        ident = it["identifier"]
        cur.execute("INSERT OR REPLACE INTO archive_recordings VALUES(?,?,?,?,?,?,?,?,?)", (
            ident, date, classify_source(it.get("source"), ident),
            (it.get("venue") or "").strip()[:200], (it.get("coverage") or "").strip()[:120],
            (float(it["avg_rating"]) if it.get("avg_rating") else None),
            int(it.get("num_reviews") or 0), int(it.get("downloads") or 0),
            f"https://archive.org/details/{ident}"))
        n += 1
    con.commit()
    # validation floors — fail loudly if upstream shape changed
    assert n >= 18000, f"expected >=18000 recordings, got {n}"
    nd = cur.execute("SELECT COUNT(DISTINCT show_date) FROM archive_recordings").fetchone()[0]
    assert nd >= 2000, f"expected >=2000 distinct shows, got {nd}"
    print(f"Built archive_recordings: {n} recordings, {nd} shows, {skipped} skipped  [validation OK]")

if __name__ == "__main__":
    if not os.path.exists(RAW_PATH):
        sys.exit(f"missing {RAW_PATH}; run scrape_archive.py first")
    con = sqlite3.connect(DB_PATH); build(con); con.close()
