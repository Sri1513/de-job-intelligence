-- migrations/012_job_milestones.sql
-- One row per lock-screen milestone per job (branch
-- friday/lockscreen-milestone-table).
--
-- Owner decision 2026-11-23: a milestone is done because the step
-- happened, not because its date passed. The lock-screen page used to
-- check milestones off from the calendar (done once the milestone
-- date passed) — that lied: with a Nov 28 deadline it showed
-- "Application submitted" checked for a job never applied to. From
-- now on the page reads scout.job_milestones, and a milestone is done
-- only when the owner or Gemini marks it done (mark_job_milestone MCP
-- tool). The seven keys match the seven rows on the page, in order:
-- saved, fit_check, package_built, package_assets (the "Resume ·
-- Cover letter · Outreach" sub-line), submitted, follow_up,
-- reply_expected.
--
-- Seed (so existing pages render exactly as before): every saved job
-- gets saved/done (done_at = saved_at); rows with a fit score get
-- fit_check/done; rows with apply_status SUBMITTED/MANUAL get
-- submitted/done (done_at = applied_at). package_built,
-- package_assets, follow_up, reply_expected have no stored state to
-- seed from — they start undone until marked. Staging marks saved/done
-- for every newly inserted job (fail-open), so the table stays complete.
--
-- Idempotent (IF NOT EXISTS + ON CONFLICT DO NOTHING = safe to run
-- repeatedly). Deleting the saved job deletes its milestones (CASCADE).
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/012_job_milestones.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
-- CI never applies migrations; this is a manual-psql step only. Run it
-- BEFORE merging the friday/lockscreen-milestone-table branch — the
-- handler reads the table, so code-first would fall back to the old
-- date-derived checks until the migration lands (the fallback keeps
-- the page up, but the table is the source of truth). Do NOT run it
-- against any database you cannot rebuild.

CREATE TABLE IF NOT EXISTS scout.job_milestones (
    job_id character varying(100) NOT NULL
        REFERENCES scout.saved_jobs(job_id) ON DELETE CASCADE,
    milestone_key TEXT NOT NULL CHECK (milestone_key IN (
        'saved', 'fit_check', 'package_built', 'package_assets',
        'submitted', 'follow_up', 'reply_expected'
    )),
    done BOOLEAN NOT NULL DEFAULT FALSE,
    done_at TIMESTAMPTZ,
    marked_by TEXT,
    PRIMARY KEY (job_id, milestone_key)
);

-- Saved: every job in the table is saved by definition.
INSERT INTO scout.job_milestones (job_id, milestone_key, done, done_at, marked_by)
SELECT job_id, 'saved', TRUE, saved_at, 'seed'
FROM scout.saved_jobs
ON CONFLICT (job_id, milestone_key) DO NOTHING;

-- Fit check: a fit score on the row means Gemini evaluated it.
INSERT INTO scout.job_milestones (job_id, milestone_key, done, done_at, marked_by)
SELECT job_id, 'fit_check', TRUE, NULL, 'seed'
FROM scout.saved_jobs
WHERE fit_score IS NOT NULL AND fit_score <> ''
ON CONFLICT (job_id, milestone_key) DO NOTHING;

-- Submitted: the application went in (submitted time is on the row).
INSERT INTO scout.job_milestones (job_id, milestone_key, done, done_at, marked_by)
SELECT job_id, 'submitted', TRUE, applied_at, 'seed'
FROM scout.saved_jobs
WHERE apply_status IN ('SUBMITTED', 'MANUAL')
ON CONFLICT (job_id, milestone_key) DO NOTHING;
