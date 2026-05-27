"""Single source of truth for lore-pipeline configuration.

Centralized so a model swap is one line and the meta table can record what
produced the vectors. Env vars override defaults for testing.
"""
import os

EMBEDDING_MODEL = os.environ.get("LORE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = int(os.environ.get("LORE_EMBEDDING_DIM", "384"))
LORE_DB_PATH = os.environ.get("LORE_DB_PATH", "/hddpool/datastore/dead_lore.db")

# chunking — tokens, approximate (we count chars/4 as cheap proxy)
CHUNK_SIZE = int(os.environ.get("LORE_CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.environ.get("LORE_CHUNK_OVERLAP", "64"))
