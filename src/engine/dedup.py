"""Semantic dedup: is this newcomer a repost of a job already saved?

Exact-key dedup (generate_job_id in pipeline_utils) misses reposts:
the same role under a new URL, a tweaked title, or a different
aggregator gets a different job_id and lands as a second row.

This module compares a new job's requirement chunks against the
stored chunk embeddings (Lesson 1's backfill) with pgvector cosine
distance.

The rule, and why it is not "best chunk wins" (measured live
2026-11-16, /tmp/diag_pairs.py):
- One chunk at 1.0000 proves NOTHING. A line of dashes became its
  own "required" chunk and matched an unrelated company's posting
  at 1.0; Amazon's standard AWS-stack bullet is verbatim in many
  Amazon postings and matched a different Amazon role at 1.0.
- So a duplicate needs AGREEMENT: one other job must match at
  least DEDUP_MIN_MATCHING_CHUNKS (2) of this job's chunks at or
  above DEDUP_THRESHOLD (0.96). A true repost matches on all its
  chunks (4/4 for the Amazon Whole Foods pair); a boilerplate twin
  matches on exactly one.
- Known edge case the rule does NOT solve: adjacent-level roles
  with a shared template (ACV ML Engineer III vs IV share 4/4
  chunks at >= 0.98). The bodies are near-identical; only the
  title differs. Whether the title should gate the verdict is a
  Step 3 decision, with this pair as the evidence.

TITLE GATE (added in Step 3): when the caller passes a title, the
verdict also requires the matched job's title to agree after
normalization (lowercase, non-alphanumerics to spaces, collapse
whitespace). Why: for a job hunt, hiding a distinct role is worse
than letting a duplicate row through — a duplicate is noise, a
hidden role is a lost opportunity. The gate keeps the Amazon
slug/li pair (same title), the Amtex pair (same title), and the
Accenture family (same title), and excludes ACV III vs IV
(titles differ). No company gate: company spellings vary
("Amtex System Inc." vs "Amtex Systems Inc.") and normalizing
them is fragile. The gate is applied per candidate BEFORE picking
the best, so a title-mismatched best can never shadow a
title-matching twin. title=None means no gate (the Step 2 demo
shape); an empty title after normalization can never verify
identity, so it returns None.

Design notes, carried over from Lessons 1-3:
- The stored embeddings ARE the cache: nothing is re-embedded per
  check. One pgvector nearest-neighbour query per new chunk, served
  by the HNSW index (vector_cosine_ops, migration 008).
- The module is fail-LOUD (it raises); the ingestion call site
  wraps it fail-open, so a dedup error never blocks ingestion.
- A job with no chunks (empty description) has no verdict:
  find_duplicate returns None. The call site must ALSO skip
  placeholder descriptions — their text is boilerplate ("Role: X
  at Y. Extracted via scraper.") and would match each other at ~1.0.
"""

import re
from typing import Any, Dict, Optional

from src.engine.chunker import chunk_jd
from src.engine.embedder import embed_texts

DEDUP_THRESHOLD = 0.96
DEDUP_MIN_MATCHING_CHUNKS = 2


def _normalize_title(title: str) -> str:
    """Lowercase, non-alphanumerics to spaces, collapse whitespace.

    "Data Engineer CO-OP" and "Data Engineer Co-Op" agree; "ML
    Engineer III, Data" and "ML Engineer IV, Data" do not.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (title or "").lower())).strip()


def _vector_literal(vec) -> str:
    """One embedding as pgvector text: '[0.12,-0.03,...]'.

    Same literal format as chunk_writer._vector_literal, written out
    here on purpose: the read path must not depend on the writer
    module. The (%s)::vector cast in the SQL does the parsing, so no
    pgvector Python package is needed.
    """
    return "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"


_BEST_MATCH_SQL = """
SELECT job_id, chunk_index, chunk_kind,
       1 - (embedding <=> (%s)::vector) AS cosine_sim
FROM scout.job_requirement_chunks
WHERE job_id <> %s
ORDER BY embedding <=> (%s)::vector
LIMIT 1;
"""


_TITLES_SQL = """
SELECT job_id, title FROM scout.saved_jobs WHERE job_id = ANY(%s);
"""


def find_best_match(conn, embedding, exclude_job_id: str) -> Optional[Dict[str, Any]]:
    """Nearest stored chunk to one embedding, from a different job.

    Returns {job_id, chunk_index, chunk_kind, cosine_sim}, or None
    when the table holds no chunk from another job. Accepts dict rows
    (get_db_connection sets dict_row) or plain tuples, like
    hybrid_search.search_evidence_for_job does.
    """
    literal = _vector_literal(embedding)
    with conn.cursor() as cur:
        cur.execute(_BEST_MATCH_SQL, (literal, exclude_job_id, literal))
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return {
            "job_id": row["job_id"],
            "chunk_index": row["chunk_index"],
            "chunk_kind": row["chunk_kind"],
            "cosine_sim": float(row["cosine_sim"]),
        }
    return {
        "job_id": row[0],
        "chunk_index": row[1],
        "chunk_kind": row[2],
        "cosine_sim": float(row[3]),
    }


def find_duplicate(
    conn,
    job_id: str,
    description: str,
    title: Optional[str] = None,
    threshold: float = DEDUP_THRESHOLD,
    min_matching_chunks: int = DEDUP_MIN_MATCHING_CHUNKS,
) -> Optional[Dict[str, Any]]:
    """Best cross-job match for a new job, or None.

    Chunks the description (chunk_jd), embeds the chunks
    (embed_texts), and asks find_best_match per chunk. Chunks are
    grouped by the job they matched: a job counts as a duplicate
    only when ONE other job matches at least min_matching_chunks
    of this job's chunks at or above the threshold. When a title
    is passed, candidates whose normalized title disagrees are
    dropped before the best is picked. The result is that job:
    {job_id, score, matching_chunks, chunk_index,
    matched_chunk_index, chunk_kind, matched_chunk_kind}.
    No chunks -> None (no verdict, never a duplicate). A title
    that normalizes to empty can never verify identity -> None.
    """
    if title is not None and not _normalize_title(title):
        return None
    chunks = chunk_jd(job_id, description or "")
    if not chunks:
        return None
    embeddings = embed_texts([c["chunk_text"] for c in chunks])
    # job_id -> {matching_chunks, best score, best chunk pair}
    per_job: Dict[str, Dict[str, Any]] = {}
    for chunk, vec in zip(chunks, embeddings):
        match = find_best_match(conn, vec, exclude_job_id=job_id)
        if match is None or match["cosine_sim"] < threshold:
            continue
        entry = per_job.setdefault(
            match["job_id"],
            {
                "matching_chunks": 0,
                "score": 0.0,
                "chunk_index": None,
                "matched_chunk_index": None,
                "chunk_kind": None,
                "matched_chunk_kind": None,
            },
        )
        entry["matching_chunks"] += 1
        if match["cosine_sim"] > entry["score"]:
            entry["score"] = match["cosine_sim"]
            entry["chunk_index"] = chunk["chunk_index"]
            entry["matched_chunk_index"] = match["chunk_index"]
            entry["chunk_kind"] = chunk["chunk_kind"]
            entry["matched_chunk_kind"] = match["chunk_kind"]
    if title is not None and per_job:
        # Title gate, one query for all candidates: a mismatched
        # best must not shadow a matching twin.
        wanted = _normalize_title(title)
        with conn.cursor() as cur:
            cur.execute(_TITLES_SQL, (list(per_job),))
            rows = cur.fetchall()
        titles: Dict[str, str] = {}
        for row in rows:
            if isinstance(row, dict):
                titles[row["job_id"]] = row["title"]
            else:
                titles[row[0]] = row[1]
        per_job = {
            candidate_id: entry
            for candidate_id, entry in per_job.items()
            if _normalize_title(titles.get(candidate_id) or "") == wanted
        }
    best_job_id = None
    best_entry: Optional[Dict[str, Any]] = None
    for candidate_id, entry in per_job.items():
        if entry["matching_chunks"] < min_matching_chunks:
            continue
        if best_entry is None or entry["score"] > best_entry["score"]:
            best_job_id, best_entry = candidate_id, entry
    if best_entry is None:
        return None
    return {"job_id": best_job_id, **best_entry}
