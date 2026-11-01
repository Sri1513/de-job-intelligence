-- migrations/004_dashboard_mcp_readonly.sql
-- Purpose: read-only database role + sanitized views for the public
-- dashboard MCP server and local LLM queries (Stage 1 of the
-- dashboard-MCP roadmap). No writes, no personal data, no raw tables.
--
-- SECURITY RULES FOR THIS FILE
-- - This file contains NO password. The role password lives ONLY in the
--   jobs VM's .env as MCP_DASHBOARD_READER_PASSWORD (that file is
--   gitignored) and is passed to psql as a variable at apply time.
--   Never paste a password into this file, into git, or into chat.
-- - The views deliberately OMIT status, sponsorship, notes, metadata,
--   apply_status and applied_at. Public reads already strip those fields
--   (see the PR #50 public-vs-token gating in src/dashboard/app.py);
--   a public MCP view must not hand them back out. Rows are filtered to
--   exclude DELETED rows, but the status value itself is never selected.
-- - The views run with the OWNER's rights (Postgres default), which is
--   what lets the reader query them without any grant on the raw tables.
--   Do NOT add security_invoker = true: that would break the reads.
-- - default_transaction_read_only is defense-in-depth only. The real
--   enforcement is the grant set: the reader has SELECT on exactly two
--   views and nothing else.
--
-- HOW TO RUN (on the jobs VM, never from this repo's CI: CI deploy does
-- not run migrations):
--   cd /opt/projects/de-job-intelligence
--   # 1. Generate the password ONCE on the VM and append it to .env:
--   #      echo "MCP_DASHBOARD_READER_PASSWORD=$(openssl rand -hex 32)" >> .env
--   #    (hex output is shell-safe. Never print or share this value.)
--   # 2. Load the .env values into the shell, then apply:
--   #      set -a; source .env; set +a
--   #      docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--   #        -v mcp_reader_password="$MCP_DASHBOARD_READER_PASSWORD" \
--   #        < migrations/004_dashboard_mcp_readonly.sql
--   #    NOTE: the compose service is `postgres` and the database is
--   #    $DB_NAME (default job_scout_db) -- not `db` / `postgres`.
--   #    This file MUST be run through psql: the :'variable' syntax below
--   #    is psql variable substitution, not plain SQL.
--   # 3. Verify (see the queries at the bottom of this file).
--
-- SCHEMA NOTES (verified against the codebase 2026-11-01, not assumed
-- from the planning thread):
-- - The jobs table is scout.saved_jobs (see migrations/001_catchup.sql
--   and src/database/helper_repo.py), NOT public.saved_jobs.
-- - Real columns: job_id, title, company, location, job_category,
--   fit_score, saved_at, job_url, description. There is NO created_at,
--   date_posted, or source column (date_posted and source live inside
--   the metadata JSON; they are intentionally left out of v1).
-- - fit_score is stored as TEXT (live /api/jobs returns "64", not 64),
--   so the metrics view casts it only after a numeric-shape check.
--   A bare avg(fit_score) would fail on this column type.
--
-- Idempotent: safe to run repeatedly (DO block, CREATE OR REPLACE VIEW,
-- repeatable GRANTs). Re-running re-sets the role password from the
-- current .env value, which is the intended rotation path.

BEGIN;

-- 1. Create the dedicated read-only role if it does not already exist.
--    No password here: it is set by the ALTER below from the psql
--    variable, which comes from the VM .env at apply time.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'mcp_dashboard_reader') THEN
        CREATE ROLE mcp_dashboard_reader WITH LOGIN;
    END IF;
END
$$;

-- Set (or rotate) the role password from the psql variable.
-- Requires: psql -v mcp_reader_password="$MCP_DASHBOARD_READER_PASSWORD"
ALTER ROLE mcp_dashboard_reader WITH PASSWORD :'mcp_reader_password';

-- 2. Guardrails on the role.
ALTER ROLE mcp_dashboard_reader SET default_transaction_read_only = on;
ALTER ROLE mcp_dashboard_reader SET statement_timeout = '2000ms';
ALTER ROLE mcp_dashboard_reader CONNECTION LIMIT 5;

-- 3. Zero-trust baseline: no access to the raw schema at all.
REVOKE ALL ON SCHEMA scout FROM mcp_dashboard_reader;
GRANT USAGE ON SCHEMA scout TO mcp_dashboard_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA scout FROM mcp_dashboard_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA scout FROM mcp_dashboard_reader;

-- 4. Sanitized per-job view. Public posting data only: no status,
--    sponsorship, notes, metadata, or application state.
CREATE OR REPLACE VIEW scout.v_dashboard_job_analytics AS
SELECT
    job_id,
    title,
    company,
    location,
    job_category,
    fit_score,
    saved_at,
    job_url,
    CASE
        WHEN description IS NOT NULL AND length(description) > 0
        THEN substring(description FROM 1 FOR 600) || '...'
        ELSE 'Description pending ingestion'
    END AS description_preview
FROM scout.saved_jobs
WHERE status != 'DELETED';

-- 5. Pre-aggregated market metrics view (one row per job category).
--    fit_score is TEXT, so average/high-match use a numeric-shape guard
--    instead of a bare cast, which would fail on non-numeric values.
CREATE OR REPLACE VIEW scout.v_dashboard_market_metrics AS
SELECT
    job_category,
    count(*) AS total_jobs_tracked,
    round(avg(
        CASE
            WHEN fit_score::text ~ '^[0-9]+(\.[0-9]+)?$'
            THEN fit_score::numeric
        END
    ), 1) AS average_fit_score,
    count(*) FILTER (
        WHERE fit_score::text ~ '^[0-9]+(\.[0-9]+)?$'
          AND fit_score::numeric >= 80
    ) AS high_match_jobs,
    count(DISTINCT company) AS unique_hiring_companies,
    max(saved_at) AS last_ingested_at
FROM scout.saved_jobs
WHERE status != 'DELETED'
GROUP BY job_category;

-- 6. Grant SELECT on exactly these two views, nothing else.
GRANT SELECT ON scout.v_dashboard_job_analytics TO mcp_dashboard_reader;
GRANT SELECT ON scout.v_dashboard_market_metrics TO mcp_dashboard_reader;

COMMIT;

-- VERIFICATION (run on the VM after applying; all three must behave
-- exactly as noted, otherwise do NOT merge this branch):
--   # A. Read access on the views (must return rows):
--   PGPASSWORD="$MCP_DASHBOARD_READER_PASSWORD" docker compose exec -T postgres \
--     psql -U mcp_dashboard_reader -d "$DB_NAME" \
--     -c "SELECT job_category, total_jobs_tracked, average_fit_score FROM scout.v_dashboard_market_metrics;"
--   # B. Write block (must ERROR -- never succeeds):
--   PGPASSWORD="$MCP_DASHBOARD_READER_PASSWORD" docker compose exec -T postgres \
--     psql -U mcp_dashboard_reader -d "$DB_NAME" \
--     -c "DELETE FROM scout.v_dashboard_job_analytics;"
--   PGPASSWORD="$MCP_DASHBOARD_READER_PASSWORD" docker compose exec -T postgres \
--     psql -U mcp_dashboard_reader -d "$DB_NAME" \
--     -c "INSERT INTO scout.saved_jobs (job_id, title) VALUES ('probe', 'probe');"
--   # C. Raw-table read block (must ERROR -- permission denied):
--   PGPASSWORD="$MCP_DASHBOARD_READER_PASSWORD" docker compose exec -T postgres \
--     psql -U mcp_dashboard_reader -d "$DB_NAME" \
--     -c "SELECT job_id FROM scout.saved_jobs LIMIT 1;"
