-- Singular test: fails (returns rows) if mart_market_metrics ever
-- diverges from the hand-written view it replaces. Category count
-- plus the summed job total must agree exactly.
select
    'category_count' as check_name,
    (select count(*) from {{ ref('mart_market_metrics') }})::text as mart_value,
    (select count(*) from scout.v_dashboard_market_metrics)::text as view_value
where (select count(*) from {{ ref('mart_market_metrics') }})
   <> (select count(*) from scout.v_dashboard_market_metrics)

union all

select
    'total_jobs_tracked' as check_name,
    (select coalesce(sum(total_jobs_tracked), 0) from {{ ref('mart_market_metrics') }})::text as mart_value,
    (select coalesce(sum(total_jobs_tracked), 0) from scout.v_dashboard_market_metrics)::text as view_value
where (select coalesce(sum(total_jobs_tracked), 0) from {{ ref('mart_market_metrics') }})
   <> (select coalesce(sum(total_jobs_tracked), 0) from scout.v_dashboard_market_metrics)
