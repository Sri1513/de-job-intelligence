-- migrations/007_telemetry_audit_tables.sql
-- Purpose: Sprint 1, Batch 1 — give LLM spend and MCP calls a home.
-- Two append-only ledger tables in the scout schema:
--   scout.llm_telemetry_ledger — one row per LLM call (tokens, latency,
--     provider/model, failover, estimated cost). Feeds the FinOps
--     "Cost per Application & Failover Telemetry" widget (Sprint 1).
--   scout.mcp_audit_log — one row per MCP tool call (who, what, scope,
--     duration, outcome). Feeds Sprint 2's audit trail and per-tool
--     OAuth scopes (scope is STORED now, ENFORCED in Sprint 2).
--
-- WHAT THIS BATCH DOES NOT DO (later batches, not here):
-- - No pgvector image swap (postgres:16-alpine stays for now; this file
--   uses plain Postgres 16 types only, so it applies cleanly today).
-- - No writers yet: nothing INSERTs into these tables until the
--   FailoverLLM telemetry hook (Sprint 1, later batch) and the MCP
--   dispatcher audit hook (Sprint 2) land. This batch only creates them.
-- - No prompts, JDs, or resume text are EVER stored here: providers,
--   models, token counts, and timings only. Cost/token data is not
--   personal data, so no public-gating redaction is needed.
--
-- HOW IT IS APPLIED: CI deploy does not run migrations (same as 001–006).
-- This file is the canonical record + the manual fallback, run on the
-- jobs VM only, never from CI:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     < migrations/007_telemetry_audit_tables.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS,
-- so re-running it is safe.
--
-- SCHEMA NOTES (why these types — read before changing them):
-- - id BIGSERIAL, not SERIAL: ledgers grow one row per call, so they
--   outgrow a 32-bit sequence far sooner than a contact-requests table.
--   Matches the 006 pattern (SERIAL PK), widened for append-only growth.
-- - created_at TIMESTAMPTZ DEFAULT now(): matches 002/006. All
--   dashboard timestamps are UTC; the UI converts to local time.
-- - correlation_id UUID: the per-request id Sprint 2's error
--   sanitization returns to the client. Same value in both tables joins
--   one MCP call to the LLM calls it triggered. Nullable in the ledger
--   (a direct/non-MCP LLM call has no MCP request); NOT NULL in the
--   audit log (every MCP call gets one).
-- - job_id TEXT, NO foreign key (deliberate): market/portfolio chat
--   calls have no job, and deleting a job must never delete history.
--   The ledger must never fail an INSERT because a job row is gone.
-- - fallback_reason vocabulary comes from the code, not invented here:
--   src/engine/llm_router.py classifies every failure as 'quota',
--   'billing', or 'transient'. The CHECK pins exactly those three.
-- - estimated_cost_usd NUMERIC(10,6), NOT float: money is never stored
--   in a float (rounding). 6 decimal places hold sub-cent free-tier
--   rates (e.g. $0.000001/token-scale pricing).
-- - scope TEXT, no CHECK yet: today the server has a single "mcp"
--   scope; Sprint 2 adds mcp:read / mcp:ingest / mcp:apply and the
--   CHECK then. Storing the column now means history exists before
--   enforcement starts.
-- - status CHECK ('ok','error'): one outcome word per row, so the
--   dashboard can COUNT(*) ... WHERE status = 'error' without parsing
--   text. error_message holds the client-safe message only — the full
--   traceback stays server-side in mcp.log (Sprint 2 rule).

CREATE TABLE IF NOT EXISTS scout.llm_telemetry_ledger (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    correlation_id UUID,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    task TEXT,
    tool_name TEXT,
    job_id TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    total_tokens INTEGER GENERATED ALWAYS AS (prompt_tokens + completion_tokens) STORED,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    fallback_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_reason TEXT CHECK (fallback_reason IS NULL OR fallback_reason IN ('quota', 'billing', 'transient')),
    estimated_cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0 CHECK (estimated_cost_usd >= 0),
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_telemetry_created_at
    ON scout.llm_telemetry_ledger (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_telemetry_provider_created
    ON scout.llm_telemetry_ledger (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_telemetry_correlation
    ON scout.llm_telemetry_ledger (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_telemetry_job
    ON scout.llm_telemetry_ledger (job_id) WHERE job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scout.mcp_audit_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    correlation_id UUID NOT NULL,
    client_id TEXT,
    tool_name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'mcp',
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    error_code TEXT,
    error_message TEXT,
    request_size_bytes INTEGER CHECK (request_size_bytes IS NULL OR request_size_bytes >= 0)
);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_created_at
    ON scout.mcp_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_correlation
    ON scout.mcp_audit_log (correlation_id);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_tool_created
    ON scout.mcp_audit_log (tool_name, created_at DESC);

-- VERIFICATION (on the VM after applying; both must return the tables):
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     -c "\d scout.llm_telemetry_ledger" \
--     -c "\d scout.mcp_audit_log" \
--     -c "SELECT count(*) FROM scout.llm_telemetry_ledger;" \
--     -c "SELECT count(*) FROM scout.mcp_audit_log;"
--   (Both counts must be 0 on first apply — no writers exist yet.
--   Re-running this file must succeed with no errors.)
