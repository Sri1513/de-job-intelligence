"""Tests for Sprint 3, Lesson 2: hybrid search + prompt wiring.

No database is needed: the search runs against a FakeCursor (same
FakePool / FakeCursor pattern as tests/test_chunk_writer.py), and the
prompt-builder test patches get_db_connection the way
tests/test_prompt_builder.py already does for the WhatsApp path.

- the SQL fuses vector (<=>) and full-text (ts_rank) rankings with RRF.
- search_evidence_for_job groups by requirement chunk and keeps top_n.
- dict rows (the app pool's row_factory=dict_row) are used as-is — the
  Step 3 live bug was zipping a dict's keys instead of its values.
- rrf_score is coerced Decimal -> float so the bundle stays JSON-safe.
- the prompt builder carries retrieved evidence + policy, and a search
  failure never breaks tailoring (fail-open).
"""

from decimal import Decimal
from unittest.mock import patch

from src.engine.hybrid_scorer import RRF_K
from src.engine.hybrid_search import _HYBRID_SEARCH_SQL, search_evidence_for_job

SEARCH_COLUMNS = (
    "chunk_index",
    "requirement_kind",
    "requirement_text",
    "source_ref",
    "source_type",
    "chunk_text",
    "evidence_skills",
    "v_rank",
    "t_rank",
    "rrf_score",
)


class FakeCursor:
    def __init__(self, job_row=None, search_rows=None, fail_search=False):
        self.job_row = job_row
        self.search_rows = search_rows or []
        self.fail_search = fail_search
        self.statements = []
        self.description = None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "job_requirement_chunks" in sql:
            if self.fail_search:
                raise RuntimeError("boom")
            self.description = [(c,) for c in SEARCH_COLUMNS]
        else:
            self.description = None

    def fetchone(self):
        return self.job_row

    def fetchall(self):
        return self.search_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tuple_row(chunk_index, rrf, ref="herc:bullet:2"):
    return (
        chunk_index,
        "required",
        "Experience with Azure Databricks.",
        ref,
        "experience_bullet",
        "Created PySpark pipelines in Databricks.",
        ["pyspark", "databricks"],
        3,
        2,
        rrf,
    )


def test_sql_fuses_both_signals_with_rrf():
    assert "scout.job_requirement_chunks" in _HYBRID_SEARCH_SQL
    assert "scout.resume_evidence_chunks" in _HYBRID_SEARCH_SQL
    assert "embedding <=>" in _HYBRID_SEARCH_SQL
    assert "ts_rank" in _HYBRID_SEARCH_SQL
    assert "LEFT JOIN text_ranked" in _HYBRID_SEARCH_SQL
    # RRF k is the same constant the hybrid scorer uses, inlined by f-string.
    assert f"1.0 / ({RRF_K} + v.v_rank)" in _HYBRID_SEARCH_SQL
    assert f"1.0 / ({RRF_K} + t.t_rank)" in _HYBRID_SEARCH_SQL
    assert RRF_K == 60


def test_search_groups_by_chunk_and_respects_top_n():
    rows = [
        _tuple_row(0, 0.03, "herc:bullet:2"),
        _tuple_row(0, 0.02, "accenture:bullet:2"),
        _tuple_row(0, 0.01, "optum:bullet:0"),
        _tuple_row(0, 0.005, "blue-yonder:bullet:1"),
        _tuple_row(1, 0.025, "herc:bullet:5"),
        _tuple_row(1, 0.015, "optum:bullet:3"),
    ]
    cursor = FakeCursor(search_rows=rows)
    result = search_evidence_for_job(FakeConnection(cursor), "j1", top_n=3)
    assert set(result) == {0, 1}
    assert [r["source_ref"] for r in result[0]] == [
        "herc:bullet:2",
        "accenture:bullet:2",
        "optum:bullet:0",
    ]
    assert len(result[1]) == 2
    assert result[0][0]["rrf_score"] == 0.03
    assert result[0][0]["requirement_text"] == "Experience with Azure Databricks."
    sql, params = cursor.statements[0]
    assert "job_requirement_chunks" in sql
    assert params == {"job_id": "j1"}


def test_search_accepts_dict_rows():
    # Regression test for the Step 3 live bug: the app pool sets
    # row_factory=dict_row, so the real path hands us dicts. Zipping a
    # dict would pair each column with its own key string instead of
    # its value — dict rows must be used as-is.
    rows = [
        dict(zip(SEARCH_COLUMNS, _tuple_row(0, 0.03))),
        dict(zip(SEARCH_COLUMNS, _tuple_row(0, 0.02, "accenture:bullet:2"))),
    ]
    result = search_evidence_for_job(FakeConnection(FakeCursor(search_rows=rows)), "j1")
    assert [r["source_ref"] for r in result[0]] == ["herc:bullet:2", "accenture:bullet:2"]
    assert result[0][0]["rrf_score"] == 0.03


def test_search_coerces_decimal_rrf_to_float():
    rows = [_tuple_row(0, Decimal("0.0317"))]
    result = search_evidence_for_job(FakeConnection(FakeCursor(search_rows=rows)), "j1")
    score = result[0][0]["rrf_score"]
    assert score == 0.0317
    assert isinstance(score, float)


def test_search_empty_job_returns_empty():
    assert search_evidence_for_job(FakeConnection(FakeCursor(search_rows=[])), "j1") == {}


def _job_row():
    return {
        "job_id": "j1",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "United States",
        "is_remote": True,
        "job_url": "https://example.invalid/j1",
        "description": (
            "Build and maintain PySpark pipelines on Databricks. "
            "Experience with Azure Databricks, Python, Spark, Git required."
        ),
        "job_category": "data_engineering",
        "metadata": {},
        "notes": "",
    }


def test_build_prompt_includes_retrieved_evidence():
    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    search_rows = [
        dict(zip(SEARCH_COLUMNS, _tuple_row(0, Decimal("0.032")))),
    ]
    cursor = FakeCursor(job_row=_job_row(), search_rows=search_rows)
    with patch(
        "src.synthesis.prompt_builder.get_db_connection",
        return_value=FakeConnection(cursor),
    ):
        bundle = build_job_tailoring_prompt("j1")
    assert "error" not in bundle
    guidelines = bundle["tailoring_guidelines"]
    assert guidelines["retrieved_evidence"] == [
        {
            "requirement_index": 0,
            "requirement_kind": "required",
            "requirement_text": "Experience with Azure Databricks.",
            "evidence": [
                {
                    "source_ref": "herc:bullet:2",
                    "source_type": "experience_bullet",
                    "chunk_text": "Created PySpark pipelines in Databricks.",
                    "rrf_score": 0.032,
                }
            ],
        }
    ]
    assert "Prefer" in guidelines["retrieved_evidence_policy"]
    assert "deterministic_bullet_ranking" in guidelines
    assert "slot_frameworks" in bundle


def test_build_prompt_search_failure_is_fail_open():
    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    cursor = FakeCursor(job_row=_job_row(), search_rows=[], fail_search=True)
    with patch(
        "src.synthesis.prompt_builder.get_db_connection",
        return_value=FakeConnection(cursor),
    ):
        bundle = build_job_tailoring_prompt("j1")
    assert "error" not in bundle
    guidelines = bundle["tailoring_guidelines"]
    assert guidelines["retrieved_evidence"] == []
    assert "retrieved_evidence_policy" in guidelines
    assert "deterministic_bullet_ranking" in guidelines
