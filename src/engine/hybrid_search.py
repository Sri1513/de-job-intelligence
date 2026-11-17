"""Lesson 2: hybrid search over the Lesson 1 chunk tables.

For one job, rank the resume evidence chunks two ways per JD requirement
chunk — vector (cosine meaning) and full-text (tsvector words) — and fuse
the two rankings with Reciprocal Rank Fusion, the same fusion the hybrid
scorer already uses (RRF_K = 60, imported, not duplicated).

Why two signals: vectors catch paraphrase ("orchestrated batch loads"
~ "scheduled ETL workflows") but can miss an exact tool name; text
search catches "dbt" exactly but misses synonyms. Each alone has a blind
spot the other covers.
"""

from __future__ import annotations

from typing import Any

from src.engine.hybrid_scorer import RRF_K

# One query, four CTEs. %(job_id)s is the only parameter (psycopg style);
# RRF_K is inlined by the f-string below so SQL and Python never disagree
# about k — it is our own constant, not user input.
_HYBRID_SEARCH_SQL = f"""
WITH job_chunks AS (
    -- The JD, already split: one row per requirement of this job.
    SELECT chunk_index, chunk_kind, chunk_text, embedding, skill_tags
    FROM scout.job_requirement_chunks
    WHERE job_id = %(job_id)s
),
vector_ranked AS (
    -- Signal 1, meaning: every evidence chunk ranked by cosine distance
    -- (<=>) to this requirement's embedding. Smallest distance = rank 1.
    -- All 66 evidence chunks rank here; nothing is filtered out.
    SELECT jc.chunk_index AS chunk_index,
           jc.chunk_kind AS requirement_kind,
           jc.chunk_text AS requirement_text,
           ec.source_ref AS source_ref,
           ec.source_type AS source_type,
           ec.chunk_text AS chunk_text,
           ec.skill_tags AS evidence_skills,
           ROW_NUMBER() OVER (
               PARTITION BY jc.chunk_index
               ORDER BY ec.embedding <=> jc.embedding, ec.source_ref
           ) AS v_rank
    FROM job_chunks jc
    CROSS JOIN scout.resume_evidence_chunks ec
),
text_ranked AS (
    -- Signal 2, words: evidence ranked by ts_rank against a query built
    -- from THIS requirement's skill tags ('delta_lake' -> 'delta & lake',
    -- tags ORed together). Only matching evidence ranks here; the rest
    -- simply get no text rank and contribute 0 to the fusion.
    -- NOTE: plainto_tsquery on the whole chunk text would AND dozens of
    -- words together and match almost nothing — tags are the right query.
    -- And to_tsquery needs explicit operators: a bare space between terms
    -- is a syntax error, which is why '_' becomes ' & ' here, not ' '.
    -- (Caught live 2026-11-16: 'data quality' with a space failed.)
    SELECT jc.chunk_index AS chunk_index,
           ec.source_ref AS source_ref,
           ROW_NUMBER() OVER (
               PARTITION BY jc.chunk_index
               ORDER BY ts_rank(ec.search_vector, to_tsquery('english', (
                   SELECT string_agg(replace(tag, '_', ' & '), ' | ')
                   FROM unnest(jc.skill_tags) AS tag
               ))) DESC, ec.source_ref
           ) AS t_rank
    FROM job_chunks jc
    CROSS JOIN scout.resume_evidence_chunks ec
    WHERE ec.search_vector @@ to_tsquery('english', (
        SELECT string_agg(replace(tag, '_', ' & '), ' | ')
        FROM unnest(jc.skill_tags) AS tag
    ))
),
fused AS (
    -- RRF: each ranker contributes 1 / (k + rank); a missing rank
    -- contributes 0. LEFT JOIN is enough — the vector side ranks every
    -- evidence chunk, so a text-only row cannot exist.
    SELECT v.chunk_index AS chunk_index,
           v.requirement_kind AS requirement_kind,
           v.requirement_text AS requirement_text,
           v.source_ref AS source_ref,
           v.source_type AS source_type,
           v.chunk_text AS chunk_text,
           v.evidence_skills AS evidence_skills,
           v.v_rank AS v_rank,
           t.t_rank AS t_rank,
           COALESCE(1.0 / ({RRF_K} + v.v_rank), 0.0)
               + COALESCE(1.0 / ({RRF_K} + t.t_rank), 0.0) AS rrf_score
    FROM vector_ranked v
    LEFT JOIN text_ranked t
      ON t.chunk_index = v.chunk_index
     AND t.source_ref = v.source_ref
)
SELECT chunk_index, requirement_kind, requirement_text, source_ref,
       source_type, chunk_text, evidence_skills, v_rank, t_rank, rrf_score
FROM fused
ORDER BY chunk_index, rrf_score DESC;
"""


def search_evidence_for_job(conn, job_id: str, top_n: int = 3) -> dict[int, list[dict[str, Any]]]:
    """Top evidence chunks per requirement chunk of one job.

    Runs the hybrid CTE on an already-open connection (the prompt builder
    holds one; this function never opens its own) and returns
    {chunk_index: [evidence rows best-first]}, at most top_n per chunk.
    Each row carries requirement_kind, requirement_text, source_ref,
    source_type, chunk_text, evidence_skills, v_rank, t_rank and the
    fused rrf_score. A job with no chunks returns {}.
    """
    with conn.cursor() as cur:
        cur.execute(_HYBRID_SEARCH_SQL, {"job_id": job_id})
        columns = [d[0] for d in cur.description]
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            # The app pool sets row_factory=dict_row, so the real path
            # hands us dicts; plain connections and test fakes hand us
            # tuples. Accept both — zipping a dict would pair each column
            # with its own key string instead of its value.
            if isinstance(row, dict):
                rows.append(dict(row))
            else:
                rows.append(dict(zip(columns, row)))
    # The SQL's numeric math returns Decimal; coerce to float so the
    # result is JSON-safe for the prompt bundle downstream.
    for row in rows:
        row["rrf_score"] = float(row["rrf_score"])
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:  # already ordered by chunk_index, rrf_score DESC
        bucket = grouped.setdefault(row["chunk_index"], [])
        if len(bucket) < top_n:
            bucket.append(row)
    return grouped
