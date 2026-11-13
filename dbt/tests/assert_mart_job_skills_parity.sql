-- Singular test: fails (returns rows) if mart_job_skills ever
-- diverges from the hand-written view it replaces
-- (scout.v_dashboard_job_skills, migrations/005).
--
-- Same frozen-input rule as the other parity tests (see
-- assert_mart_job_analytics_parity.sql): the mart is built from
-- int_job_skills, frozen at the last dbt run, while the live view
-- re-expands the mutating source table on every read. This test
-- replays the view's DISTINCT over the SAME frozen input, so it
-- checks the mart's logic, not source churn. If migrations/005
-- ever changes the view, update the `expected` CTE below to match.
-- NOTE: each UNION branch is parenthesised — a bare LIMIT before a
-- UNION is a Postgres syntax error.
with expected as (
    select distinct
        job_id,
        skill
    from {{ ref('int_job_skills') }}
    where status != 'DELETED'
)
(select
    'row_count' as check_name,
    (select count(*) from {{ ref('mart_job_skills') }})::text as mart_value,
    (select count(*) from expected)::text as view_value
where (select count(*) from {{ ref('mart_job_skills') }})
   <> (select count(*) from expected))

union all

(select
    'missing_in_mart' as check_name,
    e.job_id || ':' || e.skill as mart_value,
    null::text as view_value
from expected e
where not exists (
    select 1 from {{ ref('mart_job_skills') }} m
    where m.job_id = e.job_id and m.skill = e.skill
)
limit 5)

union all

(select
    'extra_in_mart' as check_name,
    m.job_id || ':' || m.skill as mart_value,
    null::text as view_value
from {{ ref('mart_job_skills') }} m
where not exists (
    select 1 from expected e
    where e.job_id = m.job_id and e.skill = m.skill
)
limit 5)
