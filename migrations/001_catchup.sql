-- migrations/001_catchup.sql
-- Schema catch-up for the friday/fix-session-portability branch.
--
-- Adds the apply-worker lease/retry columns that never existed on databases
-- built by earlier versions. Idempotent (IF NOT EXISTS = safe to run
-- repeatedly).
--
-- NOTE: ai_notes is NOT a real column -- the codebase stores AI notes in the
-- `notes` column (see update_job_evaluation / dashboard). get_job_by_id now
-- selects `notes AS ai_notes`; no migration needed for it.
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/001_catchup.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)

ALTER TABLE scout.saved_jobs
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_saved_jobs_apply_status_retry
    ON scout.saved_jobs (apply_status, next_retry_at);
