# tests/test_dedup.py — Sprint 3, Lesson 4.
# The semantic dedup checker (src/engine/dedup.py), the repost-location
# merge, and the stage_raw_jobs wiring. No model, no live DB: the
# embedder/chunker are stubbed and the DB is a FakeCursor, same
# pattern as tests/test_hybrid_search.py.
import numpy as np
import pytest

from src.engine.dedup import (
    _BEST_MATCH_SQL,
    _TITLES_SQL,
    _normalize_title,
    _vector_literal,
    find_best_match,
    find_duplicate,
)
from src.engine.pipeline_utils import _merge_repost_location, stage_raw_jobs

REAL_JD = """**About the role**
Northwind Traders is hiring a Data Engineer for our logistics analytics
team in Austin. You will own the nightly replenishment pipeline.

**Responsibilities**
* Build and operate Airflow DAGs that ingest POS scans via Fivetran into Snowflake, with dbt Cloud models for store-level sell-through.
* Add Monte Carlo monitors for freshness and volume anomalies.

**Requirements**
* 4+ years building batch pipelines in Python and SQL; production Airflow experience required.
* Hands-on dbt Cloud and Snowflake, including streams and tasks.
"""


class FakeCursor:
    """Statements recorded; fetchone/fetchall dispatch on SQL text."""

    def __init__(self, existing_row=None, metadata_row=None, title_rows=None, best_row=None):
        self.existing_row = existing_row
        self.metadata_row = metadata_row
        self.title_rows = title_rows or []
        self.best_row = best_row
        self.statements = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        if "SELECT description, metadata FROM saved_jobs" in self._last_sql:
            return self.existing_row
        if "SELECT metadata FROM saved_jobs" in self._last_sql:
            return self.metadata_row
        if "FROM scout.job_requirement_chunks" in self._last_sql:
            return self.best_row
        return None

    def fetchall(self):
        if "WHERE job_id = ANY" in self._last_sql:
            return self.title_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chunks(n):
    return [
        {
            "job_id": "new-job",
            "chunk_index": i,
            "chunk_kind": "required",
            "chunk_text": f"chunk {i} text",
            "skill_tags": [],
        }
        for i in range(n)
    ]


def _match(job_id, score):
    return {
        "job_id": job_id,
        "chunk_index": 0,
        "chunk_kind": "required",
        "cosine_sim": score,
    }


def _stub_chunk_embed(monkeypatch, n):
    monkeypatch.setattr("src.engine.dedup.chunk_jd", lambda job_id, desc: _chunks(n))
    monkeypatch.setattr(
        "src.engine.dedup.embed_texts",
        lambda texts, **kwargs: np.zeros((len(texts), 384), dtype=np.float32),
    )


def _stub_best_matches(monkeypatch, matches):
    """find_best_match returns the canned matches in call order."""
    calls = {"i": 0}

    def _fake(conn, embedding, exclude_job_id):
        m = matches[calls["i"]]
        calls["i"] += 1
        return m

    monkeypatch.setattr("src.engine.dedup.find_best_match", _fake)


def _job(**overrides):
    job = {
        "title": "Data Engineer, Logistics Analytics",
        "company": "Northwind Traders",
        "job_url": "https://www.linkedin.com/jobs/view/9999999998",
        "location": "Austin, TX",
        "description": REAL_JD,
        "source": "LinkedIn",
    }
    job.update(overrides)
    return job


# --- literals, titles, SQL ---


def test_vector_literal_format():
    assert _vector_literal([0.5, -0.25]) == "[0.500000,-0.250000]"


def test_normalize_title():
    assert _normalize_title("Data Engineer CO-OP") == _normalize_title("Data Engineer Co-Op")
    assert _normalize_title("ML Engineer III, Data") != _normalize_title("ML Engineer IV, Data")
    assert _normalize_title("") == ""


def test_best_match_sql():
    assert "scout.job_requirement_chunks" in _BEST_MATCH_SQL
    assert "embedding <=>" in _BEST_MATCH_SQL
    assert "job_id <> %s" in _BEST_MATCH_SQL
    assert "LIMIT 1" in _BEST_MATCH_SQL
    assert "scout.saved_jobs" in _TITLES_SQL
    assert "WHERE job_id = ANY" in _TITLES_SQL


# --- find_best_match ---


def test_find_best_match_dict_row():
    cursor = FakeCursor(
        best_row={"job_id": "j1", "chunk_index": 2, "chunk_kind": "required", "cosine_sim": 0.99}
    )
    result = find_best_match(FakeConnection(cursor), [0.1] * 384, "new-job")
    assert result["job_id"] == "j1"
    assert result["cosine_sim"] == 0.99
    assert isinstance(result["cosine_sim"], float)
    assert "new-job" in cursor.statements[0][1]


def test_find_best_match_tuple_row():
    cursor = FakeCursor(best_row=("j1", 2, "required", 0.99))
    result = find_best_match(FakeConnection(cursor), [0.1] * 384, "new-job")
    assert result == {
        "job_id": "j1",
        "chunk_index": 2,
        "chunk_kind": "required",
        "cosine_sim": 0.99,
    }


def test_find_best_match_no_rows():
    cursor = FakeCursor(best_row=None)
    assert find_best_match(FakeConnection(cursor), [0.1] * 384, "new-job") is None


# --- find_duplicate: the agreement rule ---


def test_find_duplicate_two_chunks_same_job(monkeypatch):
    _stub_chunk_embed(monkeypatch, 2)
    _stub_best_matches(monkeypatch, [_match("twin-1", 0.99), _match("twin-1", 0.98)])
    result = find_duplicate(FakeConnection(FakeCursor()), "new-job", REAL_JD)
    assert result["job_id"] == "twin-1"
    assert result["matching_chunks"] == 2
    assert result["score"] == 0.99


def test_find_duplicate_single_chunk_is_not_a_duplicate(monkeypatch):
    # One chunk at 1.0 is boilerplate until proven otherwise (the
    # dashes chunk, the shared AWS bullet): it must not carry a verdict.
    _stub_chunk_embed(monkeypatch, 2)
    _stub_best_matches(monkeypatch, [_match("twin-1", 1.0), _match("twin-1", 0.5)])
    assert find_duplicate(FakeConnection(FakeCursor()), "new-job", REAL_JD) is None


def test_find_duplicate_split_across_jobs_is_not_a_duplicate(monkeypatch):
    _stub_chunk_embed(monkeypatch, 2)
    _stub_best_matches(monkeypatch, [_match("job-x", 0.99), _match("job-y", 0.99)])
    assert find_duplicate(FakeConnection(FakeCursor()), "new-job", REAL_JD) is None


def test_find_duplicate_empty_description(monkeypatch):
    _stub_chunk_embed(monkeypatch, 0)
    assert find_duplicate(FakeConnection(FakeCursor()), "new-job", "") is None


def test_find_duplicate_empty_title_is_none(monkeypatch):
    called = {"n": 0}

    def _chunk(job_id, desc):
        called["n"] += 1
        return []

    monkeypatch.setattr("src.engine.dedup.chunk_jd", _chunk)
    assert find_duplicate(FakeConnection(FakeCursor()), "new-job", REAL_JD, title="") is None
    assert called["n"] == 0


# --- find_duplicate: the title gate ---


def test_title_gate_keeps_matching_drops_mismatched(monkeypatch):
    _stub_chunk_embed(monkeypatch, 4)
    _stub_best_matches(
        monkeypatch,
        [_match("job-x", 0.99), _match("job-x", 0.98), _match("job-y", 1.0), _match("job-y", 0.99)],
    )
    cursor = FakeCursor(title_rows=[("job-x", "Data Engineer"), ("job-y", "Data Engineer II")])
    result = find_duplicate(FakeConnection(cursor), "new-job", REAL_JD, title="Data Engineer")
    # Y scores higher but its title disagrees: the gate drops it, X wins.
    assert result["job_id"] == "job-x"


def test_title_gate_no_title_means_no_gate(monkeypatch):
    _stub_chunk_embed(monkeypatch, 4)
    _stub_best_matches(
        monkeypatch,
        [_match("job-x", 0.99), _match("job-x", 0.98), _match("job-y", 1.0), _match("job-y", 0.99)],
    )
    cursor = FakeCursor(title_rows=[("job-x", "Data Engineer"), ("job-y", "Data Engineer II")])
    result = find_duplicate(FakeConnection(cursor), "new-job", REAL_JD, title=None)
    assert result["job_id"] == "job-y"


# --- _merge_repost_location ---


def test_merge_repost_location_appends():
    cursor = FakeCursor(
        metadata_row={"metadata": {"locations": [{"city": "Seattle, WA", "url": "u1"}]}}
    )
    _merge_repost_location(cursor, "twin-1", "Austin, TX", "u2")
    updates = [s for s, _ in cursor.statements if "UPDATE saved_jobs SET metadata" in s]
    assert len(updates) == 1
    payload = cursor.statements[-1][1][0]
    import json

    locations = json.loads(payload)["locations"]
    assert {"city": "Austin, TX", "url": "u2"} in locations
    assert len(locations) == 2


def test_merge_repost_location_noop_when_city_present():
    cursor = FakeCursor(
        metadata_row={"metadata": {"locations": [{"city": "Austin, TX", "url": "u1"}]}}
    )
    _merge_repost_location(cursor, "twin-1", "Austin, TX", "u2")
    assert not any("UPDATE saved_jobs" in s for s, _ in cursor.statements)


def test_merge_repost_location_noop_when_row_missing():
    cursor = FakeCursor(metadata_row=None)
    _merge_repost_location(cursor, "twin-1", "Austin, TX", "u2")
    assert not any("UPDATE saved_jobs" in s for s, _ in cursor.statements)


# --- stage_raw_jobs wiring ---


def _has_insert(cursor):
    return any("INSERT INTO saved_jobs" in s for s, _ in cursor.statements)


def test_stage_skips_semantic_duplicate(monkeypatch):
    monkeypatch.setattr(
        "src.engine.pipeline_utils.find_duplicate",
        lambda conn, job_id, desc, title=None: {"job_id": "twin-1"},
    )
    cursor = FakeCursor(metadata_row={"metadata": {"locations": []}})
    stats = stage_raw_jobs(FakeConnection(cursor), [_job()])
    assert stats["saved"] == 0
    assert stats["skipped"] == 1
    assert not _has_insert(cursor)
    assert any("UPDATE saved_jobs SET metadata" in s for s, _ in cursor.statements)


def test_stage_fail_open_when_dedup_raises(monkeypatch):
    def _boom(conn, job_id, desc, title=None):
        raise RuntimeError("dedup down")

    monkeypatch.setattr("src.engine.pipeline_utils.find_duplicate", _boom)
    cursor = FakeCursor()
    stats = stage_raw_jobs(FakeConnection(cursor), [_job()])
    assert stats["saved"] == 1
    assert _has_insert(cursor)


def test_stage_placeholder_skips_checker(monkeypatch):
    called = {"n": 0}

    def _spy(conn, job_id, desc, title=None):
        called["n"] += 1
        return None

    monkeypatch.setattr("src.engine.pipeline_utils.find_duplicate", _spy)
    cursor = FakeCursor()
    stats = stage_raw_jobs(FakeConnection(cursor), [_job(description="Data Engineer role.")])
    assert called["n"] == 0
    assert stats["saved"] == 1
    assert _has_insert(cursor)


def test_stage_new_job_saved_when_no_duplicate(monkeypatch):
    monkeypatch.setattr(
        "src.engine.pipeline_utils.find_duplicate",
        lambda conn, job_id, desc, title=None: None,
    )
    cursor = FakeCursor()
    stats = stage_raw_jobs(FakeConnection(cursor), [_job()])
    assert stats["saved"] == 1
    assert stats["skipped"] == 0
    assert _has_insert(cursor)
