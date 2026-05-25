"""
build_db.py - Load gdshowsdb YAML into a normalized SQLite database.

Source: github.com/jefmsmit/gdshowsdb (MIT). One YAML file per year, plus
song_refs.yaml mapping canonical song name -> UUID.

This is the STRUCTURED SPINE. Setlist/counting/trivia questions are answered
by SQL against this, never by an LLM guessing.

--- Show date extraction (Plex join) ---
When joining Plex albums to this database, extract the show date from the
album *title*, never from Plex's `year` metadata field. Plex stores the
release year (e.g. 1993 for Dick's Picks Vol. 1), not the show year (1973).
Using the year field will silently mis-date every Dick's Picks / Dave's Picks
release in the library.

The title-based regex `^(\d{4}-\d{2}-\d{2})` covers the vast majority of
live releases. For multi-date titles ("1969-01-24 - 26", "1972-03-21 & 27"),
take the first date as the primary key. Albums that don't lead with a full
YYYY-MM-DD (month-only boots like "1968-06 Carousel", studio albums) should
resolve to NULL and be logged — never fall back to the year field.
Expected NULL count is ~10-20 compilations/box sets; significantly more
indicates the title parser needs an additional pattern.
"""
import yaml, glob, os, sqlite3

DATA_DIR = "gddata/data/gdshowsdb"
DB_PATH = os.environ.get("DB_PATH", "/hddpool/datastore/dead.db")

def year_files():
    return sorted(
        f for f in glob.glob(os.path.join(DATA_DIR, "*.yaml"))
        if os.path.basename(f)[:4].isdigit()
    )

def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE songs (
        uuid TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE shows (
        date TEXT PRIMARY KEY,        -- canonical key: YYYY-MM-DD
        uuid TEXT,
        venue TEXT, city TEXT, state TEXT, country TEXT
    );
    CREATE TABLE performances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_date TEXT NOT NULL REFERENCES shows(date),
        set_num INTEGER NOT NULL,     -- 1-based: set 1, set 2, encore...
        position INTEGER NOT NULL,    -- order within the set
        song_uuid TEXT REFERENCES songs(uuid),
        song_name TEXT NOT NULL,      -- denormalized for convenience
        segued_out INTEGER NOT NULL   -- 1 if this song segues (>) into next
    );
    CREATE INDEX idx_perf_song ON performances(song_uuid);
    CREATE INDEX idx_perf_date ON performances(show_date);
    """)

    # song reference table
    refs = yaml.safe_load(open(os.path.join(DATA_DIR, "song_refs.yaml")))
    for entry in refs:
        (name, uuid), = entry.items()
        cur.execute("INSERT OR IGNORE INTO songs(uuid,name) VALUES(?,?)", (uuid, name))

    # shows + performances
    n_shows = n_perf = 0
    for f in year_files():
        d = yaml.safe_load(open(f)) or {}
        for raw_date, show in d.items():
            date = raw_date.replace("/", "-")        # 1977/02/26 -> 1977-02-26
            cur.execute(
                "INSERT OR REPLACE INTO shows VALUES(?,?,?,?,?,?)",
                (date, show.get(":uuid"), show.get(":venue"),
                 show.get(":city"), show.get(":state"), show.get(":country")))
            n_shows += 1
            for set_idx, s in enumerate(show.get(":sets") or [], start=1):
                for pos, song in enumerate(s.get(":songs") or [], start=1):
                    cur.execute(
                        "INSERT INTO performances"
                        "(show_date,set_num,position,song_uuid,song_name,segued_out)"
                        " VALUES(?,?,?,?,?,?)",
                        (date, set_idx, pos, song.get(":uuid"),
                         song.get(":name"), 1 if song.get(":segued") else 0))
                    n_perf += 1

    con.commit()
    # validation floors — fail loudly if upstream data changed shape
    assert n_shows >= 2358, f"expected >=2358 shows, got {n_shows}"
    assert n_perf  >= 39774, f"expected >=39774 performances, got {n_perf}"
    assert len(refs) >= 526, f"expected >=526 songs, got {len(refs)}"
    print(f"Built {DB_PATH}: {n_shows} shows, {n_perf} performances, "
          f"{len(refs)} songs  [validation OK]")
    con.close()

if __name__ == "__main__":
    build()
