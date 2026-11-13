-- models/intermediate/int_job_skills.sql
-- Silver, Batch 6. The skills grain: one row per (job_id, skill).
-- This is the expensive step in the whole project — expanding the
-- matched_skills JSON array on every job — so it lives here once,
-- incrementally, instead of being re-run inside every mart and every
-- dashboard read (the hand-written v_dashboard_job_skills view does
-- the expansion on EVERY read).
--
-- Logic is copied exactly from migrations/005 (the view's contract):
-- expand metadata->'matched_skills' when it is an array, else an
-- empty array; lowercase; DISTINCT so a repeated skill on one
-- posting counts once; drop empty strings ('' is not a skill, it is
-- a data-quality rule, not a mart decision).
--
-- INCREMENTAL: delete+insert on (job_id, skill), watermarked on
-- saved_at (carried as a bookkeeping column so the watermark works;
-- Gold does not select it). Same honest limit as int_saved_jobs:
-- new jobs arrive nightly; in-place metadata edits to old rows wait
-- for the weekly full refresh. NO status filtering here — Gold
-- excludes DELETED; Silver keeps the faithful mirror of staging.

{{
    config(
        materialized='incremental',
        unique_key=['job_id', 'skill'],
        incremental_strategy='delete+insert',
        on_schema_change='sync_all_columns'
    )
}}

select distinct
    job_id,
    lower(skill) as skill,
    status,
    saved_at
from {{ ref('stg_saved_jobs') }},
    lateral jsonb_array_elements_text(
        case
            when jsonb_typeof(metadata::jsonb -> 'matched_skills') = 'array'
            then metadata::jsonb -> 'matched_skills'
            else '[]'::jsonb
        end
    ) as s(skill)
where skill <> ''

{% if is_incremental() %}
  -- Parentheses matter: without them the OR would escape the
  -- skill <> '' filter and let empty skills through.
  and (saved_at > (select coalesce(max(saved_at), '1900-01-01') from {{ this }})
   or saved_at is null)
{% endif %}
