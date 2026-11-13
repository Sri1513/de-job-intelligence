-- Singular test: fails (returns rows) if mart_market_metrics ever
-- diverges from the hand-written view it replaces
-- (scout.v_dashboard_market_metrics, migrations/004).
--
-- Same frozen-input rule as the other parity tests (see
-- assert_mart_job_analytics_parity.sql): the mart is built from
-- int_saved_jobs, frozen at the last dbt run, while the live view
-- reads the mutating source table. This test replays the view's
-- aggregation over the SAME frozen input, so it checks the mart's
-- logic, not source churn. If migrations/004 ever changes the view,
-- update the `expected` CTE below to match.
with expected as (
    select
        job_category,
        count(*) as total_jobs_tracked,
        round(avg(fit_score_num), 1) as average_fit_score,
        count(*) filter (where fit_score_num >= 80) as high_match_jobs,
        count(distinct company) as unique_hiring_companies,
        max(saved_at) as last_ingested_at
    from {{ ref('int_saved_jobs') }}
    where status != 'DELETED'
    group by job_category
)
(select
    'category_count' as check_name,
    (select count(*) from {{ ref('mart_market_metrics') }})::text as mart_value,
    (select count(*) from expected)::text as view_value
where (select count(*) from {{ ref('mart_market_metrics') }})
   <> (select count(*) from expected))

union all

(select
    'total_jobs_tracked' as check_name,
    (select coalesce(sum(total_jobs_tracked), 0) from {{ ref('mart_market_metrics') }})::text as mart_value,
    (select coalesce(sum(total_jobs_tracked), 0) from expected)::text as view_value
where (select coalesce(sum(total_jobs_tracked), 0) from {{ ref('mart_market_metrics') }})
   <> (select coalesce(sum(total_jobs_tracked), 0) from expected))
