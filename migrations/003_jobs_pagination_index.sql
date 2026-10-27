-- Keyset-pagination index for /api/jobs (newest-first pages).
--
-- NOTE: CI deploy does not run migrations, so this file alone does NOT
-- land the index. The dashboard creates it itself on startup (see
-- _ensure_jobs_pagination_index in src/dashboard/app.py, fail-open).
-- This file is the canonical record plus the manual fallback:
--   PGPASSWORD=$DB_PASSWORD psql -h $DB_HOST -U $DB_USER -d $DB_NAME \
--     -f migrations/003_jobs_pagination_index.sql
CREATE INDEX IF NOT EXISTS idx_saved_jobs_category_saved_at
    ON saved_jobs (job_category, saved_at DESC NULLS LAST, job_id DESC);
