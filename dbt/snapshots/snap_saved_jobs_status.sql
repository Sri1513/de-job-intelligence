{% snapshot snap_saved_jobs_status %}
{{
    config(
      target_schema='analytics',
      unique_key='job_id',
      strategy='check',
      check_cols=['status', 'apply_status'],
    )
}}
-- snapshots/snap_saved_jobs_status.sql
-- SCD-2 history of job status, Batch 6. saved_jobs has NO history
-- today: status and apply_status flip in place (SAVED -> SUBMITTED
-- -> MANUAL ...), and yesterday's value is gone forever. This
-- snapshot records every transition — one row per (job_id, version)
-- with dbt_valid_from / dbt_valid_to — so "when did this job move,
-- and what was it before" is answerable.
--
-- CHECK strategy, not timestamp: saved_jobs has no updated_at
-- column (verified 2026-11-12), so dbt compares the two check
-- columns on every run and opens a new version only when one of
-- them changed. First run snapshots all 2,330 rows as version 1.
--
-- Why NOT outreach_tracking: it is already CDC Type 2 by design —
-- the app versions it itself (version / is_current /
-- effective_start / effective_end in helper_repo.py). Snapshotting
-- it would duplicate history the pipeline already keeps. The gap
-- was saved_jobs status, and that is what this covers.

select
    job_id,
    status,
    apply_status,
    saved_at,
    applied_at
from {{ source('scout', 'saved_jobs') }}

{% endsnapshot %}
