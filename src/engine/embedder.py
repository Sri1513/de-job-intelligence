"""Sprint 3, Lesson 1 — turn text into embeddings.

An embedding is a 384-number fingerprint of a piece of text, produced by
the all-MiniLM-L6-v2 model. Two texts with similar meaning get fingerprints
that point in a similar direction; cosine similarity (the angle between
them) is the score. Every vector this module returns is L2-normalized
(length 1.0), so cosine similarity is just the dot product — the same
contract the chunk tables were built for (migration 008) and the same
encoding the hybrid scorer already uses (normalize_embeddings=True).

The model is loaded lazily (on first call) and cached, mirroring
src/engine/hybrid_scorer.py's _get_sbert(). If the model cannot load,
embed_texts() raises instead of returning empty vectors: a backfill that
silently wrote NULL embeddings would be worse than a loud failure.
"""

import logging
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_BATCH_SIZE = 32

_model = None
_model_failed = False


def _get_model():
    """Load the embedding model once and cache it; None if unavailable."""
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL)
        logger.info("Embedding model loaded: %s", EMBEDDING_MODEL)
    except Exception as exc:  # environment dependent (no package / no model)
        logger.warning("Embedding model unavailable: %s", exc)
        _model_failed = True
        return None
    return _model


def embed_texts(texts: Sequence[str], batch_size: int = EMBEDDING_BATCH_SIZE) -> np.ndarray:
    """Encode texts into normalized embeddings, shape (n, 384).

    Each row has length 1.0, so cosine similarity between two rows is
    their dot product. Raises RuntimeError when the model cannot load —
    there is no meaningful fallback for an embedding step. Empty input
    never touches the model: it returns a (0, 384) array right away.
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    model = _get_model()
    if model is None:
        raise RuntimeError(
            f"Embedding model {EMBEDDING_MODEL!r} unavailable; "
            "install sentence-transformers and download the model first."
        )
    return model.encode(
        list(texts),
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )


def embed_one(text: str) -> np.ndarray:
    """Encode a single text; returns one normalized vector of length 384."""
    return embed_texts([text])[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two normalized vectors: just their dot product.

    1.0 means identical direction, 0.0 means unrelated, -1.0 means opposite.
    """
    return float(np.dot(a, b))
