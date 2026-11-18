"""Tests for src/evals/ragas_scorer.py (Sprint 4, Lesson 3).

No real LLM calls here: the unit tests cover the sample mapping,
the judge seam, and the fail-open behaviour. The real evaluate()
call is exercised live on de-lab (Lesson 3 Step 4), not in CI.
"""

import pytest

from src.evals.ragas_scorer import (
    build_sample,
    make_judge,
    score_draft,
    score_sample,
)


def test_build_sample_maps_question_contexts_response():
    sample = build_sample(
        "Data Engineer at 4P",
        ["Built pipelines with PySpark", "Tuned Spark SQL joins"],
        "- Built pipelines with PySpark\n",
    )
    assert sample.user_input == "Data Engineer at 4P"
    assert sample.retrieved_contexts == [
        "Candidate: Sri Omkar D.",
        "Built pipelines with PySpark",
        "Tuned Spark SQL joins",
    ]
    assert sample.response == "- Built pipelines with PySpark\n"


def test_make_judge_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown judge kind"):
        make_judge("openai")


def test_score_sample_empty_draft_returns_nones_without_calling_judge():
    sample = build_sample("Data Engineer", ["Built pipelines"], "   ")
    assert score_sample(sample, judge=None, embeddings=None) == {
        "faithfulness": None,
        "answer_relevancy": None,
    }


def test_score_sample_broken_judge_returns_nones_instead_of_raising():
    sample = build_sample("Data Engineer", ["Built pipelines"], "- Built pipelines\n")
    assert score_sample(sample, judge=object(), embeddings=object()) == {
        "faithfulness": None,
        "answer_relevancy": None,
    }


def test_score_draft_unknown_judge_kind_returns_nones(caplog):
    with caplog.at_level("WARNING", logger="src.evals.ragas_scorer"):
        assert score_draft(
            "Data Engineer", ["Built pipelines"], "- Built pipelines\n", judge_kind="nope"
        ) == {
            "faithfulness": None,
            "answer_relevancy": None,
        }
    # Regression (Lesson 3 Step 4): the overnight run's 22 failures
    # printed an EMPTY message, hiding the exception class. The
    # warning must name the type.
    assert "ValueError" in caplog.text


def test_score_sample_calls_metrics_directly(monkeypatch):
    """Regression test (Lesson 3 Step 4): RAGAS 0.4 collections
    metrics do NOT go through evaluate() — evaluate() rejects them
    ("must be initialised metric objects"). score_sample calls each
    metric's .score() directly with the sample's fields.
    """
    from types import SimpleNamespace

    seen = {}

    class _FakeFaithfulness:
        def __init__(self, llm=None, **kwargs):
            seen["faith_llm"] = llm

        def score(self, **kwargs):
            seen["faith_kwargs"] = kwargs
            return SimpleNamespace(value=0.9)

    class _FakeRelevancy:
        def __init__(self, llm=None, embeddings=None, **kwargs):
            seen["rel_llm"] = llm
            seen["rel_emb"] = embeddings

        def score(self, **kwargs):
            seen["rel_kwargs"] = kwargs
            return SimpleNamespace(value=0.8)

    monkeypatch.setattr("ragas.metrics.collections.Faithfulness", _FakeFaithfulness)
    monkeypatch.setattr("ragas.metrics.collections.AnswerRelevancy", _FakeRelevancy)
    sample = build_sample("Data Engineer", ["Built pipelines"], "- Built pipelines\n")
    result = score_sample(sample, judge=object(), embeddings=object())
    assert result == {"faithfulness": 0.9, "answer_relevancy": 0.8}
    assert seen["faith_kwargs"] == {
        "user_input": "Data Engineer",
        "response": "- Built pipelines\n",
        "retrieved_contexts": ["Candidate: Sri Omkar D.", "Built pipelines"],
    }
    assert seen["rel_kwargs"] == {
        "user_input": "Data Engineer",
        "response": "- Built pipelines\n",
    }
