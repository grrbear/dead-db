-- dead_lore.db schema. Sibling of dead.db; joins by show_date string.
-- Rebuild policy: build_lore_db.py is idempotent at (source, source_id) grain.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,        -- 'lia', 'wikipedia', 'deadcast'
    source_id   TEXT NOT NULL,        -- stable id within source (URL, slug)
    title       TEXT,
    url         TEXT,
    published   TEXT,                 -- ISO date if known
    fetched_at  TEXT NOT NULL,        -- ISO datetime of fetch
    raw_text    TEXT NOT NULL,        -- cleaned plain text, no HTML
    metadata    TEXT,                 -- JSON: per-doc metadata (e.g. book author/year/genre). NULL for corpora that don't use it.
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,    -- 0-based position within doc
    text            TEXT NOT NULL,
    -- bridges back to dead.db (extracted at normalize time):
    mentioned_dates TEXT,                -- JSON array of YYYY-MM-DD
    mentioned_songs TEXT,                -- JSON array of canonical song names
    era             TEXT,                -- '60s'|'pigpen'|'keith'|'brent'|'bruce'|NULL
    section         TEXT,                -- source section heading this chunk came from, NULL if flat
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

-- Vector table is created in db.py after sqlite-vec is loaded; can't be in a
-- plain .sql file because the virtual-table syntax requires the extension.
