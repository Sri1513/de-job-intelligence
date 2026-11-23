-- migrations/009_posted_at.sql
-- Foundation for the 7-day job-deadline system (branch
-- friday/job-posted-date-capture).
--
-- Adds the REAL posted date of each saved job. The scraper now captures it
-- per source (JobSpy date_posted, Dice postedDate, LinkedIn alert relative
-- dates) and stores it here; the deadline is derived later as
-- posted_at + 7 days. Idempotent (IF NOT EXISTS = safe to run repeatedly).
--
-- Deliberately NO backfill: rows ingested before this column existed have
-- no trustworthy posted date, and the saved/ingested date is NOT a proxy
-- for it (owner decision 2026-11-22). Old rows stay NULL and opt out of
-- deadline derivation until a re-stage heals them.
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/009_posted_at.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
-- CI never applies migrations; this is a manual-psql step only. Do NOT run
-- it against any database you cannot rebuild.

ALTER TABLE scout.saved_jobs
    ADD COLUMN IF NOT EXISTS posted_at DATE;

CREATE INDEX IF NOT EXISTS idx_saved_jobs_posted_at
    ON scout.saved_jobs (posted_at);
