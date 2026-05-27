"""SQLite connection helper. Loads sqlite-vec and ensures schema exists."""
import sqlite3
import os
from pathlib import Path
import sqlite_vec  # pip install sqlite-vec

from .config import LORE_DB_PATH, EMBEDDING_MODEL, EMBEDDING_DIM

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str = LORE_DB_PATH, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded. Creates parent dir if needed."""
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql + create vector table. Idempotent.

    Hard-fails if the DB was built with a different embedding model than
    config currently specifies — mismatched vectors silently produce wrong
    retrieval, so we'd rather refuse to open than corrupt the index.
    """
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBEDDING_DIM}]
        )
    """)
    existing = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if existing.get("embedding_model") and existing["embedding_model"] != EMBEDDING_MODEL:
        raise RuntimeError(
            f"DB was built with {existing['embedding_model']} but config says "
            f"{EMBEDDING_MODEL}. Drop {LORE_DB_PATH} or change LORE_EMBEDDING_MODEL."
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?), (?, ?)",
        ("embedding_model", EMBEDDING_MODEL, "embedding_dim", str(EMBEDDING_DIM))
    )
    conn.commit()
