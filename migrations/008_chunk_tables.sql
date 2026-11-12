-- migrations/008_chunk_tables.sql
-- Purpose: Sprint 1, Batch 4 — give chunked JD requirements and verified
-- resume evidence a home, with pgvector embeddings for Sprint 3's hybrid RAG.
--
--   scout.job_requirement_chunks — one row per JD section-chunk, linked to
--     scout.saved_jobs. Chunks come from the deterministic splitter that
--     already exists in src/engine/jd_skill_extractor.py (_split_sections):
--     section names are exactly 'required' / 'preferred' /
--     'responsibilities' / 'other'. Sprint 3 populates these rows, embeds
--     each chunk with all-MiniLM-L6-v2 (384 dims, normalized — see
--     src/engine/hybrid_scorer.py, which encodes with
--     normalize_embeddings=True), and tags canonical skills.
--   scout.resume_evidence_chunks — one row per VERIFIED resume bullet
--     (config/company_frameworks.json experience[].highlights, 5 clients,
--     11 bullets total) plus one row per framework entry
--     (company_frameworks.json frameworks[], name + description). These
--     are the only texts the tailoring step may cite — a generated bullet
--     must trace to a source_ref here (Sprint 4's faithfulness gate).
--
-- WHAT THIS BATCH DOES NOT DO (Sprint 3, not here):
-- - No writers yet: nothing INSERTs into these tables until the Sprint 3
--   chunker + embedder + backfill land. This batch only creates them.
-- - No embeddings are computed here: the embedding columns start NULL and
--   are filled by the Sprint 3 backfill. The HNSW indexes build instantly
--   on empty tables.
-- - No prompts or full JDs are stored beyond what saved_jobs already
--   holds: chunk_text is a slice of the JD description already in the DB.
--
-- WHY THE EXTENSION GOES IN FIRST, UNQUALIFIED:
-- CREATE EXTENSION installs the `vector` type into the current schema
-- (public by default). The app connects with the default search_path
-- ($user, public), so plain `vector(384)` resolves. Installing it into
-- the scout schema instead would hide the type unless every connection
-- set search_path — don't do that.
--
-- COSINE, NOT L2: hybrid_scorer.py normalizes every embedding, so cosine
-- similarity is the right metric — hence vector_cosine_ops on both HNSW
-- indexes. A normalized vector's cosine similarity is just its dot
-- product, which is also what the in-process scorer computes today.
--
-- HOW IT IS APPLIED: CI deploy does not run migrations (same as 001–007).
-- This file is the canonical record + the manual fallback, run on the
-- jobs VM only, never from CI:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     < migrations/008_chunk_tables.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
--
-- Idempotent: CREATE EXTENSION IF NOT EXISTS + CREATE TABLE IF NOT EXISTS
-- + CREATE INDEX IF NOT EXISTS, so re-running it is safe. Re-running does
-- NOT wipe chunks: the UNIQUE keys let a backfill re-run with
-- ON CONFLICT DO NOTHING / DO UPDATE instead of duplicating rows.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- scout.job_requirement_chunks — JD sections, one row per chunk
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scout.job_requirement_chunks (
    id BIGSERIAL PRIMARY KEY,
    -- Link to the job. ON DELETE CASCADE: deleting a job deletes its
    -- chunks — chunks are derived data, they have no life without the JD.
    -- job_id is TEXT in saved_jobs (e.g. 'li-4473528772', 'dice_...') and
    -- unique there (ON CONFLICT (job_id) DO NOTHING), so the FK is legal.
    job_id TEXT NOT NULL REFERENCES scout.saved_jobs (job_id) ON DELETE CASCADE,
    -- Order of the chunk inside the JD (0, 1, 2, ...). Together with
    -- job_id this is the natural key: re-chunking the same JD reproduces
    -- the same (job_id, chunk_index) pairs.
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    -- Which JD section this chunk came from. Values are pinned to the
    -- section names _split_sections() in jd_skill_extractor.py returns —
    -- a chunker change that invents a new section name fails loudly here
    -- instead of silently storing an unweighted chunk.
    chunk_kind TEXT NOT NULL
        CHECK (chunk_kind IN ('required', 'preferred', 'responsibilities', 'other')),
    -- The chunk text itself: one JD section (or part of one, if a section
    -- is split further in Sprint 3). NOT NULL — an empty chunk is dropped
    -- by the chunker, never stored.
    chunk_text TEXT NOT NULL,
    -- all-MiniLM-L6-v2 embedding, 384 dims, L2-normalized (matches
    -- hybrid_scorer.py). NULL until the Sprint 3 backfill embeds it —
    -- a chunk can exist (and be keyword-searched) before it is embedded.
    embedding vector(384),
    -- Full-text search vector, kept in sync by Postgres itself: any
    -- INSERT/UPDATE of chunk_text recomputes it. coalesce guards the
    -- (impossible, but cheap) NULL text. Powers the keyword half of
    -- Sprint 3's hybrid retrieval; the GIN index below makes it fast.
    search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED,
    -- Canonical skill ids found in this chunk by the deterministic
    -- taxonomy matcher (jd_skill_extractor.py). Empty array, never NULL,
    -- so overlap queries (skill_tags && ARRAY['...']) work with no
    -- NULL handling. Filled by Sprint 3, not by this migration.
    skill_tags TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, chunk_index)
);

-- HNSW (approximate nearest neighbour) on the embedding: Sprint 3's
-- semantic retrieval. vector_cosine_ops because embeddings are normalized
-- (see header). Builds instantly while the table is empty.
CREATE INDEX IF NOT EXISTS idx_job_req_chunks_embedding
    ON scout.job_requirement_chunks
    USING hnsw (embedding vector_cosine_ops);
-- GIN on the generated tsvector: the keyword half of hybrid retrieval.
CREATE INDEX IF NOT EXISTS idx_job_req_chunks_search
    ON scout.job_requirement_chunks
    USING gin (search_vector);
-- Lookup by job (the common read: "give me this JD's chunks in order").
-- The UNIQUE (job_id, chunk_index) already indexes job_id, but an
-- explicit index documents the access path and survives a future
-- UNIQUE change.
CREATE INDEX IF NOT EXISTS idx_job_req_chunks_job_id
    ON scout.job_requirement_chunks (job_id);

-- ---------------------------------------------------------------------------
-- scout.resume_evidence_chunks — verified resume bullets + frameworks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scout.resume_evidence_chunks (
    id BIGSERIAL PRIMARY KEY,
    -- Stable, human-readable key for the source text. Bullets:
    -- '<client-slug>:bullet:<n>' (e.g. 'optum:bullet:0' — the first
    -- highlight of the Optum entry in company_frameworks.json).
    -- Frameworks: 'framework:<slug>' (e.g.
    -- 'framework:lakehouse-medallion-architecture'). UNIQUE so a
    -- re-run of the Sprint 3 backfill can ON CONFLICT (source_ref)
    -- DO UPDATE instead of duplicating evidence.
    source_ref TEXT NOT NULL UNIQUE,
    -- What kind of evidence this is. 'experience_bullet' = one highlight
    -- from experience[]; 'framework' = one entry from frameworks[].
    -- Sprint 4's faithfulness gate reads this to say which kind of
    -- source a generated bullet traces to.
    source_type TEXT NOT NULL
        CHECK (source_type IN ('experience_bullet', 'framework')),
    -- Experience client for bullets ('Optum', 'Herc Rentals',
    -- 'Blue Yonder', 'Accenture', 'Thomson Reuters'). NULL for
    -- framework rows — a framework belongs to no single client
    -- (frameworks[].used_in lists several).
    client TEXT,
    -- Role + period from the experience entry, for bullets
    -- (e.g. role 'Data Engineer', period 'Oct 2025 - Present').
    -- NULL for framework rows. Kept as plain text, exactly as the
    -- resume prints them — no parsing, no normalization.
    role TEXT,
    period TEXT,
    -- Framework name for framework rows (e.g.
    -- 'Lakehouse / Medallion architecture'). NULL for bullets.
    framework_name TEXT,
    -- The citable text: the bullet itself, or the framework
    -- description. This is what Sprint 3 embeds and Sprint 4 checks
    -- generated bullets against. NOT NULL — evidence with no text
    -- is not evidence.
    chunk_text TEXT NOT NULL,
    -- Same embedding contract as job_requirement_chunks: 384 dims,
    -- normalized, NULL until the Sprint 3 backfill.
    embedding vector(384),
    search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED,
    -- Canonical skill ids in this evidence (deterministic matcher,
    -- Sprint 3). Empty array default, never NULL.
    skill_tags TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_resume_ev_chunks_embedding
    ON scout.resume_evidence_chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_resume_ev_chunks_search
    ON scout.resume_evidence_chunks
    USING gin (search_vector);
-- Lookup by client ("all Optum evidence") and by kind
-- ("all framework evidence") — the two reads Sprint 3/4 make.
CREATE INDEX IF NOT EXISTS idx_resume_ev_chunks_client
    ON scout.resume_evidence_chunks (client);
CREATE INDEX IF NOT EXISTS idx_resume_ev_chunks_source_type
    ON scout.resume_evidence_chunks (source_type);
