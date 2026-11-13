-- Singular test: fails (returns rows) if mart_job_analytics ever
-- diverges from the hand-written view it replaces
-- (scout.v_dashboard_job_analytics, migrations/004).
--
-- HOW IT COMPARES (and why not the live view): the mart is built
-- from int_saved_jobs, which is FROZEN at the last dbt run, while
-- the live view reads scout.saved_jobs as it is RIGHT NOW. The
-- source table mutates between runs — Batch 6 diagnosis (2026-11-12)
-- proved rows churn in and out of saved_jobs on a ~2-minute cycle
-- (same job_id md5-identical at 01:27 UTC, absent at 01:29 UTC;
-- no DELETE exists in the application code, writer unidentified).
-- Comparing the frozen mart to the live view therefore fails on
-- source churn, not on mart error — the first version of this test
-- did exactly that (5 phantom rows, all in int, none in live).
-- So this test replays the view's logic over the SAME frozen input
-- the mart was built from (int_saved_jobs). It answers the question
-- Batch 6 owns: "given the same input rows, does the mart produce
-- exactly the view's output?" Source staleness is a separate,
-- documented property (weekly full refresh; the snapshot records
-- status history). If migrations/004 ever changes the view, update
-- the `expected` CTE below to match.
-- NOTE: each UNION branch is parenthesised — a bare LIMIT before a
-- UNION is a Postgres syntax error.
with expected as (
    select
        job_id,
        title,
        company,
        location,
        job_category,
        fit_score,
        saved_at,
        job_url_clean as job_url,
        description_preview
    from {{ ref('int_saved_jobs') }}
    where status != 'DELETED'
)
(select
    'row_count' as check_name,
    (select count(*) from {{ ref('mart_job_analytics') }})::text as mart_value,
    (select count(*) from expected)::text as view_value
where (select count(*) from {{ ref('mart_job_analytics') }})
   <> (select count(*) from expected))

union all

(select
    'missing_in_mart' as check_name,
    e.job_id as mart_value,
    null::text as view_value
from expected e
where not exists (
    select 1 from {{ ref('mart_job_analytics') }} m
    where m.job_id = e.job_id
)
limit 5)

union all

(select
    'extra_in_mart' as check_name,
    m.job_id as mart_value,
    null::text as view_value
from {{ ref('mart_job_analytics') }} m
where not exists (
    select 1 from expected e
    where e.job_id = m.job_id
)
limit 5)
