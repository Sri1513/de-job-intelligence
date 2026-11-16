"""Sprint 3, Lesson 1 — write chunks + embeddings into the chunk tables.

The chunker (Step 3) makes the chunks; the embedder (Step 2) makes the
vectors; this module puts them together into Postgres.

Database pattern, mirrored from src/core/llm_telemetry.py: get the
shared pool with get_pool(), open a connection with a 2-second wait
cap, and SET LOCAL statement_timeout so a stuck statement dies after
5 seconds instead of hanging the backfill. One deliberate difference
from telemetry: telemetry is fail-OPEN (a missed observation must never
break the request it observes), but this writer is fail-LOUD — if the
database is down, the backfill raises. There is no meaningful fallback
for a write that IS the job.

Two more design choices to notice:
- Idempotency: both INSERTs end in ON CONFLICT ... DO UPDATE, so a
  re-run refreshes the rows instead of duplicating them. The whole
  backfill runs in one transaction: if anything fails, nothing is
  half-written.
- No pgvector Python package: the embedding goes in as a text literal
  like '[0.12,-0.03,...]' with a (%s)::vector cast in the SQL. That
  is the format pgvector's input function reads.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

import numpy as np

from src.core.database import get_pool
from src.engine.chunker import chunk_frameworks, chunk_jd, parse_resume_markdown
from src.engine.embedder import EMBEDDING_BATCH_SIZE, embed_texts

logger = logging.getLogger(__name__)

DEFAULT_FRAMEWORKS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "frameworks" / "company_frameworks.json"
)

_JOB_CHUNKS_SQL = """
INSERT INTO scout.job_requirement_chunks
    (job_id, chunk_index, chunk_kind, chunk_text, embedding, skill_tags)
VALUES (%s, %s, %s, %s, (%s)::vector, %s)
ON CONFLICT (job_id, chunk_index) DO UPDATE SET
    chunk_kind = EXCLUDED.chunk_kind,
    chunk_text = EXCLUDED.chunk_text,
    embedding = EXCLUDED.embedding,
    skill_tags = EXCLUDED.skill_tags
"""

_EVIDENCE_SQL = """
INSERT INTO scout.resume_evidence_chunks
    (source_ref, source_type, client, role, period, framework_name,
     chunk_text, embedding, skill_tags)
VALUES (%s, %s, %s, %s, %s, %s, %s, (%s)::vector, %s)
ON CONFLICT (source_ref) DO UPDATE SET
    source_type = EXCLUDED.source_type,
    client = EXCLUDED.client,
    role = EXCLUDED.role,
    period = EXCLUDED.period,
    framework_name = EXCLUDED.framework_name,
    chunk_text = EXCLUDED.chunk_text,
    embedding = EXCLUDED.embedding,
    skill_tags = EXCLUDED.skill_tags
"""

_FETCH_JOBS_SQL = """
SELECT job_id, description FROM scout.saved_jobs
WHERE description IS NOT NULL AND description <> ''
ORDER BY job_id
"""


def _vector_literal(vec: np.ndarray) -> str:
    """A numpy vector as the text pgvector reads: '[0.12,-0.03,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


@contextmanager
def _open_connection(pool) -> Iterator:
    """One connection with a bounded wait and a 5s statement timeout.

    pool.connection() is itself a context manager — it hands out the
    connection on __enter__ and returns it to the pool on __exit__ —
    so this helper wraps it rather than calling it bare.
    """
    with pool.connection(timeout=2) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
        yield conn


def write_job_chunks(conn, chunks: Sequence[Dict], embeddings: np.ndarray) -> int:
    """Insert one job's chunks (with their embeddings). Returns row count."""
    if not chunks:
        return 0
    params = [
        (
            c["job_id"],
            c["chunk_index"],
            c["chunk_kind"],
            c["chunk_text"],
            _vector_literal(embeddings[i]),
            c["skill_tags"],
        )
        for i, c in enumerate(chunks)
    ]
    with conn.cursor() as cur:
        cur.executemany(_JOB_CHUNKS_SQL, params)
    return len(chunks)


def write_evidence_chunks(conn, chunks: Sequence[Dict], embeddings: np.ndarray) -> int:
    """Insert evidence chunks (bullets + frameworks). Returns row count."""
    if not chunks:
        return 0
    params = [
        (
            c["source_ref"],
            c["source_type"],
            c["client"],
            c["role"],
            c["period"],
            c["framework_name"],
            c["chunk_text"],
            _vector_literal(embeddings[i]),
            c["skill_tags"],
        )
        for i, c in enumerate(chunks)
    ]
    with conn.cursor() as cur:
        cur.executemany(_EVIDENCE_SQL, params)
    return len(chunks)


def fetch_jobs(conn) -> List[Dict]:
    """All saved jobs that have a description to chunk."""
    with conn.cursor() as cur:
        cur.execute(_FETCH_JOBS_SQL)
        return [{"job_id": row[0], "description": row[1]} for row in cur.fetchall()]


def backfill_jobs(pool=None, jobs: Optional[List[Dict]] = None, job_batch_size: int = 100) -> Dict:
    """Chunk + embed + write every JD. Returns {"jobs": n, "chunks": m}.

    Jobs are processed in batches of `job_batch_size`: chunk the batch,
    embed all its texts in one model call, write the rows — so the model
    runs efficiently and a failure loses at most one batch's work inside
    the single transaction. Pass `jobs` to backfill a given list (tests
    do this); otherwise jobs are fetched from saved_jobs.
    """
    pool = pool or get_pool()
    total_chunks = 0
    with _open_connection(pool) as conn:
        job_list = jobs if jobs is not None else fetch_jobs(conn)
        for start in range(0, len(job_list), job_batch_size):
            batch = job_list[start : start + job_batch_size]
            batch_chunks: List[Dict] = []
            for job in batch:
                batch_chunks.extend(chunk_jd(job["job_id"], job.get("description") or ""))
            if batch_chunks:
                embeddings = embed_texts(
                    [c["chunk_text"] for c in batch_chunks],
                    batch_size=EMBEDDING_BATCH_SIZE,
                )
                total_chunks += write_job_chunks(conn, batch_chunks, embeddings)
            logger.info(
                "backfill jobs %d/%d, chunks %d", start + len(batch), len(job_list), total_chunks
            )
    return {"jobs": len(job_list), "chunks": total_chunks}


def backfill_evidence(
    pool=None, md_text: Optional[str] = None, frameworks_data: Optional[Dict] = None
) -> Dict:
    """Chunk + embed + write the resume evidence. Returns counts."""
    pool = pool or get_pool()
    chunks = parse_resume_markdown(md_text or "") + chunk_frameworks(frameworks_data or {})
    with _open_connection(pool) as conn:
        if chunks:
            embeddings = embed_texts(
                [c["chunk_text"] for c in chunks],
                batch_size=EMBEDDING_BATCH_SIZE,
            )
            write_evidence_chunks(conn, chunks, embeddings)
    bullets = sum(1 for c in chunks if c["source_type"] == "experience_bullet")
    frameworks = len(chunks) - bullets
    return {"bullets": bullets, "frameworks": frameworks, "chunks": len(chunks)}


def run_backfill(
    pool=None, resume_md_path: Optional[str] = None, frameworks_path: Optional[str] = None
) -> Dict:
    """Read the real files and run both backfills. Returns combined counts.

    The frameworks file lives in the repo (default path). The resume
    markdown lives OUTSIDE the repo (the goal workspace), so its path
    is required — there is no honest default.
    """
    if not resume_md_path:
        raise ValueError("resume_md_path is required: the master resume lives outside the repo")
    md_text = Path(resume_md_path).read_text(encoding="utf-8")
    fw_path = Path(frameworks_path) if frameworks_path else DEFAULT_FRAMEWORKS_PATH
    frameworks_data = json.loads(fw_path.read_text(encoding="utf-8"))
    jobs = backfill_jobs(pool=pool)
    evidence = backfill_evidence(pool=pool, md_text=md_text, frameworks_data=frameworks_data)
    return {
        "jobs": jobs["jobs"],
        "job_chunks": jobs["chunks"],
        "bullets": evidence["bullets"],
        "frameworks": evidence["frameworks"],
        "evidence_chunks": evidence["chunks"],
    }
