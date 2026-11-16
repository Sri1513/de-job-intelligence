"""Tests for Sprint 3, Lesson 1: the embedder.

- encode returns shape (n, 384) with every row normalized to length 1.0.
- cosine similarity behaves as taught: identical texts score 1.0, a
  related pair scores clearly higher than an unrelated pair.
- empty input returns an empty (0, 384) array instead of failing.

The tests skip when the embedding model cannot load (no package, no
network for the first download), so CI stays green offline.
"""

import numpy as np
import pytest

from src.engine import embedder


def _embed_or_skip(texts):
    try:
        return embedder.embed_texts(texts)
    except RuntimeError:
        pytest.skip("embedding model unavailable")


def test_embed_texts_shape_and_norm():
    embs = _embed_or_skip(
        [
            "Built PySpark ETL pipelines on Databricks.",
            "Baked cookies on Sunday.",
        ]
    )
    assert embs.shape == (2, 384)
    norms = np.linalg.norm(embs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_embed_one_shape_and_norm():
    try:
        vec = embedder.embed_one("Built PySpark ETL pipelines on Databricks.")
    except RuntimeError:
        pytest.skip("embedding model unavailable")
    assert vec.shape == (384,)
    assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_ranking():
    texts = [
        "Built PySpark ETL pipelines on Databricks for IoT telemetry.",
        "Developed Spark data pipelines on Databricks for equipment sensor data.",
        "Baked chocolate chip cookies on Sunday afternoon.",
    ]
    embs = _embed_or_skip(texts)
    related = embedder.cosine_similarity(embs[0], embs[1])
    unrelated = embedder.cosine_similarity(embs[0], embs[2])
    assert related > 0.5
    assert unrelated < 0.3
    assert related > unrelated
    assert embedder.cosine_similarity(embs[0], embs[0]) == pytest.approx(1.0)


def test_embed_texts_empty():
    embs = embedder.embed_texts([])
    assert embs.shape == (0, 384)
