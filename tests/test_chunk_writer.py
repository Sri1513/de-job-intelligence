"""Tests for Sprint 3, Lesson 1: the chunk writer.

No database is needed: the pool is faked (same FakePool / FakeCursor
pattern as tests/test_llm_telemetry.py), and embed_texts is stubbed to
return zero vectors so the tests never load the model.

- the vector literal is the exact text pgvector's ::vector cast reads.
- writes go out as one executemany with ON CONFLICT ... DO UPDATE,
  so a re-run refreshes instead of duplicating.
- empty chunk lists write nothing and return 0.
- backfill counts jobs/chunks/bullets/frameworks honestly.
- run_backfill refuses to run without the resume path (it lives
  outside the repo, so there is no honest default).
"""

import numpy as np
import pytest

from src.engine import chunk_writer


class FakeCursor:
    def __init__(self, rows=None):
        self.statements = []  # ("execute"|"executemany", sql, params)
        self.rows = rows or []

    def execute(self, sql, params=None):
        self.statements.append(("execute", sql, params))

    def executemany(self, sql, params):
        self.statements.append(("executemany", sql, list(params)))

    def fetchall(self):
        return self.rows

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


class FakePool:
    """Stands in for psycopg_pool.ConnectionPool."""

    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self, timeout=None):
        return FakeConnection(self._cursor)


def _fake_embed(monkeypatch):
    def fake(texts, batch_size=32):
        return np.zeros((len(texts), 384), dtype=np.float32)

    monkeypatch.setattr(chunk_writer, "embed_texts", fake)


JOB_CHUNKS = [
    {
        "job_id": "j1",
        "chunk_index": 0,
        "chunk_kind": "required",
        "chunk_text": "Python and SQL required.",
        "skill_tags": ["python", "sql"],
    },
    {
        "job_id": "j1",
        "chunk_index": 1,
        "chunk_kind": "preferred",
        "chunk_text": "dbt is a plus.",
        "skill_tags": ["dbt"],
    },
]

EVIDENCE_CHUNKS = [
    {
        "source_ref": "optum:bullet:0",
        "source_type": "experience_bullet",
        "client": "Optum",
        "role": "Data Engineer",
        "period": "Oct 2025 - Present",
        "framework_name": None,
        "chunk_text": "Migrated ETL to AWS Glue.",
        "skill_tags": ["glue"],
    },
]


def test_vector_literal():
    assert chunk_writer._vector_literal(np.array([0.5, -0.25], dtype=np.float32)) == "[0.5,-0.25]"
    assert chunk_writer._vector_literal(np.zeros(3)) == "[0.0,0.0,0.0]"


def test_write_job_chunks():
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    embs = np.zeros((2, 384), dtype=np.float32)
    assert chunk_writer.write_job_chunks(conn, JOB_CHUNKS, embs) == 2
    assert len(cursor.statements) == 1
    kind, sql, params = cursor.statements[0]
    assert kind == "executemany"
    assert "ON CONFLICT (job_id, chunk_index) DO UPDATE" in sql
    assert "(%s)::vector" in sql
    first = params[0]
    assert first[0] == "j1" and first[1] == 0 and first[2] == "required"
    assert first[4].startswith("[0.0,")
    assert first[5] == ["python", "sql"]


def test_write_job_chunks_empty():
    cursor = FakeCursor()
    assert chunk_writer.write_job_chunks(FakeConnection(cursor), [], np.zeros((0, 384))) == 0
    assert cursor.statements == []


def test_write_evidence_chunks():
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    embs = np.zeros((1, 384), dtype=np.float32)
    assert chunk_writer.write_evidence_chunks(conn, EVIDENCE_CHUNKS, embs) == 1
    kind, sql, params = cursor.statements[0]
    assert kind == "executemany"
    assert "ON CONFLICT (source_ref) DO UPDATE" in sql
    assert params[0][0] == "optum:bullet:0"
    assert params[0][1] == "experience_bullet"


def test_fetch_jobs():
    cursor = FakeCursor(rows=[("j1", "desc one"), ("j2", "desc two")])
    jobs = chunk_writer.fetch_jobs(FakeConnection(cursor))
    assert jobs == [
        {"job_id": "j1", "description": "desc one"},
        {"job_id": "j2", "description": "desc two"},
    ]


def test_backfill_jobs(monkeypatch):
    _fake_embed(monkeypatch)
    cursor = FakeCursor()
    pool = FakePool(cursor)
    jobs = [
        {"job_id": "j1", "description": "Requirements\nPython and SQL."},
        {"job_id": "j2", "description": "About the role\nBuild pipelines."},
    ]
    result = chunk_writer.backfill_jobs(pool=pool, jobs=jobs)
    assert result == {"jobs": 2, "chunks": 2}
    kinds = [s[0] for s in cursor.statements]
    assert kinds[0] == "execute"  # SET LOCAL statement_timeout
    assert "statement_timeout" in cursor.statements[0][1]
    assert any(s[0] == "executemany" for s in cursor.statements)


def test_backfill_jobs_empty(monkeypatch):
    _fake_embed(monkeypatch)
    cursor = FakeCursor()
    assert chunk_writer.backfill_jobs(pool=FakePool(cursor), jobs=[]) == {"jobs": 0, "chunks": 0}


def test_backfill_evidence(monkeypatch):
    _fake_embed(monkeypatch)
    cursor = FakeCursor()
    md = "### Optum, Florida — Data Engineer, Oct 2025 - Present\n\n- Migrated ETL to AWS Glue.\n- Cut the batch to 3:00 AM.\n"
    fw = {
        "companies": {
            "herc_rentals": {
                "process_framework": {
                    "phase_1_ingestion": {
                        "phase_name": "Ingestion",
                        "daily_activity": "Extract with Python.",
                    }
                }
            }
        }
    }
    result = chunk_writer.backfill_evidence(pool=FakePool(cursor), md_text=md, frameworks_data=fw)
    assert result == {"bullets": 2, "frameworks": 1, "chunks": 3}


def test_run_backfill_requires_resume_path():
    with pytest.raises(ValueError, match="resume_md_path is required"):
        chunk_writer.run_backfill(resume_md_path=None)


def test_run_backfill(monkeypatch, tmp_path):
    _fake_embed(monkeypatch)
    cursor = FakeCursor()
    monkeypatch.setattr(chunk_writer, "get_pool", lambda: FakePool(cursor))
    md_file = tmp_path / "resume.md"
    md_file.write_text(
        "### Optum, Florida — Data Engineer, Oct 2025 - Present\n\n- Migrated ETL to AWS Glue.\n"
    )
    fw_file = tmp_path / "fw.json"
    fw_file.write_text('{"companies": {}}')
    result = chunk_writer.run_backfill(resume_md_path=str(md_file), frameworks_path=str(fw_file))
    assert result == {
        "jobs": 0,
        "job_chunks": 0,
        "bullets": 1,
        "frameworks": 0,
        "evidence_chunks": 1,
    }
