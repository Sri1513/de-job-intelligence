-- migrations/005_dashboard_mcp_skills_view.sql
-- Purpose: sanitized per-job skill view for the public MCP surface
-- (Step 2: jobs://metrics/top-skills and the query_jobs_by_criteria
-- skill filter). Same boundary as migration 004: public posting data
-- only, no status, sponsorship, notes, metadata, or application state.
--
-- PREREQUISITE: migration 004 must be applied first -- it creates the
-- mcp_dashboard_reader role and grants USAGE on the scout schema. This
-- file only adds one view and one grant; it creates no role and holds
-- no password.
--
-- HOW IT WORKS: matched_skills lives inside the metadata JSON of each
-- saved_jobs row (see src/engine/pipeline_utils.py). The view expands
-- that array one row per (job, skill), lowercased and deduplicated, so
-- counts never double-count a repeated skill on one posting. Rows
-- without a skills array contribute nothing. The metadata::jsonb cast
-- works whether the column is json or jsonb; the CASE guard keeps rows
-- with a missing or non-array value from failing the expansion.
--
-- Idempotent: CREATE OR REPLACE VIEW + repeatable GRANT.
--
-- Run on the VM (after 004, same pattern):
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     < migrations/005_dashboard_mcp_skills_view.sql
-- (No password variable needed: this file sets no password.)

CREATE OR REPLACE VIEW scout.v_dashboard_job_skills AS
SELECT DISTINCT
    job_id,
    lower(skill) AS skill
FROM scout.saved_jobs,
    LATERAL jsonb_array_elements_text(
        CASE
            WHEN jsonb_typeof(metadata::jsonb -> 'matched_skills') = 'array'
            THEN metadata::jsonb -> 'matched_skills'
            ELSE '[]'::jsonb
        END
    ) AS s(skill)
WHERE status != 'DELETED'
  AND skill <> '';

GRANT SELECT ON scout.v_dashboard_job_skills TO mcp_dashboard_reader;

-- VERIFICATION (on the VM after applying):
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     -c "SELECT skill, count(*) FROM scout.v_dashboard_job_skills GROUP BY skill ORDER BY count DESC LIMIT 10;"
--   PGPASSWORD="$MCP_DASHBOARD_READER_PASSWORD" docker compose exec -T postgres \
--     psql -U mcp_dashboard_reader -d "$DB_NAME" \
--     -c "SELECT count(*) FROM scout.v_dashboard_job_skills;"
--   (First query must return skill counts; second must succeed for the
--   reader while SELECT on scout.saved_jobs itself still fails.)
