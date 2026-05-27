"""Embedding wrapper. Lazy-loads the model on first call."""
from functools import lru_cache
import numpy as np
from sentence_transformers import SentenceTransformer

from .config import EMBEDDING_MODEL, EMBEDDING_DIM


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    # CPU-only; on arrstack this is fine for our corpus size.
    m = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
    actual = m.get_embedding_dimension()
    if actual != EMBEDDING_DIM:
        raise RuntimeError(
            f"Model {EMBEDDING_MODEL} produces dim {actual}, config says {EMBEDDING_DIM}"
        )
    return m


def embed(texts: list[str], *, batch_size: int = 32) -> np.ndarray:
    """Returns float32 array, shape (len(texts), EMBEDDING_DIM).

    Embeddings are L2-normalized — bge models are trained with normalization,
    and sqlite-vec's default distance assumes unit vectors for cosine.
    """
    if not texts:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
    vecs = _model().encode(
        texts, batch_size=batch_size, show_progress_bar=False,
        normalize_embeddings=True,
    )
    return vecs.astype(np.float32)
