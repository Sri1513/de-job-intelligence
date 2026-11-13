-- models/intermediate/int_saved_jobs.sql
-- Silver, Batch 6. The enriched job grain: one row per job_id, all
-- staging columns plus the two derivations Gold needs in more than
-- one place (fit_score_num, description_preview), so the marts stay
-- thin SELECTs and the guard logic lives in exactly one file.
--
-- INCREMENTAL: delete+insert on job_id, watermarked on saved_at.
-- Live data has NO updated_at column (verified 2026-11-12: saved_jobs
-- carries saved_at/applied_at/claimed_at/next_retry_at only), so new
-- rows are the only thing an incremental run can see. That is the
-- honest limit of this model:
--   - New jobs (saved_at > max seen) arrive nightly. Correct.
--   - In-place edits to OLD rows (a status flip, a fit_score change)
--     are invisible until a full refresh. The weekly Sunday
--     full-refresh in the schedule exists for exactly this.
--   - Status history is NOT this model's job: the snapshot
--     (snapshots/snap_saved_jobs_status.sql) records every status /
--     apply_status transition, so history survives even when the
--     incremental run misses the edit.
-- The `or saved_at is null` guard reprocesses un-timestamped rows
-- every run instead of silently dropping them (live count of NULL
-- saved_at is 0 as of 2026-11-12, so this costs nothing today).
-- At 2,330 rows a full rebuild takes ~3s; incremental here is about
-- the pattern (your Trigent 3-day-lookback shape) and the nightly
-- habit, not speed.
--
-- NO status filtering here — same rule as staging. DELETED rows stay
-- in Silver; excluding them is a mart decision.

{{
    config(
        materialized='incremental',
        unique_key='job_id',
        incremental_strategy='delete+insert',
        on_schema_change='sync_all_columns'
    )
}}

select
    job_id,
    job_url,
    job_url_clean,
    had_tracking_params,
    title,
    title_contains_url,
    title_length,
    company,
    company_clean,
    company_key,
    location,
    is_remote,
    salary_min,
    salary_max,
    fit_score,
    -- Numeric fit score, NULL when the text is not a number. Same
    -- regex guard the hand-written market-metrics view uses, so
    -- avg()/>= comparisons never cast a non-numeric value. Live data
    -- is 100% numeric today (0 non-numeric of 2,330); the guard is
    -- for the next bad ingestion, not today's data.
    case
        when fit_score::text ~ '^[0-9]+(\.[0-9]+)?$'
        then fit_score::numeric
    end as fit_score_num,
    description,
    -- Same preview rule as v_dashboard_job_analytics: first 600
    -- chars + '...', or the pending-ingestion fallback.
    case
        when description is not null and length(description) > 0
        then substring(description from 1 for 600) || '...'
        else 'Description pending ingestion'
    end as description_preview,
    metadata,
    employment_type,
    sponsorship,
    job_category,
    ai_status,
    status,
    apply_status,
    applied_at,
    saved_at
from {{ ref('stg_saved_jobs') }}

{% if is_incremental() %}
where saved_at > (select coalesce(max(saved_at), '1900-01-01') from {{ this }})
   or saved_at is null
{% endif %}
