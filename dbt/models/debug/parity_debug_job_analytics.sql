-- TEMPORARY diagnostic model — DELETE after Batch 6 diagnosis.
-- Materializes the exact parity-test query as a table, so the rows
-- it returns DURING a dbt build are persisted and inspectable
-- afterwards. The test itself only reports a count, which is why
-- the 5 build-time rows could never be seen.
{{
    config(materialized='table')
}}

(select
    'row_count' as check_name,
    (select count(*) from {{ ref('mart_job_analytics') }})::text as mart_value,
    (select count(*) from scout.v_dashboard_job_analytics)::text as view_value
where (select count(*) from {{ ref('mart_job_analytics') }})
   <> (select count(*) from scout.v_dashboard_job_analytics))

union all

(select
    'missing_in_mart' as check_name,
    v.job_id as mart_value,
    null::text as view_value
from scout.v_dashboard_job_analytics v
where not exists (
    select 1 from {{ ref('mart_job_analytics') }} m
    where m.job_id = v.job_id
)
limit 5)

union all

(select
    'extra_in_mart' as check_name,
    m.job_id as mart_value,
    null::text as view_value
from {{ ref('mart_job_analytics') }} m
where not exists (
    select 1 from scout.v_dashboard_job_analytics v
    where v.job_id = m.job_id
)
limit 5)
