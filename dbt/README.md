# dbt — de-job-intelligence transformation layer

Sprint 1, Batch 5. dbt Core turns the raw `scout.*` tables into clean,
tested marts. It does not ingest (the Python pipeline owns that) and it
does not replace Spark.

## Where it runs

**de-lab (the Lenovo E480), not the Oracle VM.** dbt is a CLI runner,
not a server: install it on de-lab, keep the project here in git, and
point it at the VM's Postgres over Tailscale (`100.118.69.90:5432` —
verified reachable from de-lab 2026-11-12, ~21ms). Runs are batch
(nightly is fine); if de-lab is down, a run just doesn't happen and the
dashboard keeps serving the last good marts.

## Install (on de-lab, once)

```bash
# Pinned matched pair — do NOT bare `pip install dbt-postgres`
# (it once pulled a 1.13.0 beta core; there is no 1.12 postgres
# adapter, so 1.11.x is the newest usable pair).
pip3 install --user "dbt-core~=1.11.0" "dbt-postgres==1.11.0"
dbt --version   # expect core 1.11.x + postgres 1.11.0
```

## Connect (one-time setup)

**1. Create the dbt role on the VM** (your step — run inside the
postgres container; the password is yours to choose, never in chat
or in git):

```bash
cd /opt/projects/de-job-intelligence
docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME"
```

```sql
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS analytics_dev;
CREATE ROLE dbt_runner LOGIN PASSWORD '<choose-a-strong-password>';
GRANT USAGE ON SCHEMA scout TO dbt_runner;
GRANT SELECT ON ALL TABLES IN SCHEMA scout TO dbt_runner;
ALTER DEFAULT PRIVILEGES IN SCHEMA scout GRANT SELECT ON TABLES TO dbt_runner;
GRANT USAGE, CREATE ON SCHEMA analytics TO dbt_runner;
GRANT USAGE, CREATE ON SCHEMA analytics_dev TO dbt_runner;
```

Least privilege, on purpose: dbt can read `scout` and write only its
own `analytics` / `analytics_dev` schemas. It cannot touch the app
tables, and it never gets the app user's password.

**2. Profiles file on de-lab** (once):

```bash
mkdir -p ~/.dbt
cp <repo>/dbt/profiles.yml.example ~/.dbt/profiles.yml
export DBT_PASSWORD='<the dbt_runner password>'
```

**3. Check the connection from de-lab:**

```bash
cd <repo>/dbt
dbt debug   # all checks green = the role, network and profile agree
```

If `dbt debug` fails on auth, the likely cause is pg_hba on the VM
not allowing the Tailscale IP — that is a VM-side check, not a dbt
problem.

## Run

```bash
cd <repo>/dbt
dbt run     # build all models
dbt test    # run the tests in schema.yml
dbt docs generate && dbt docs serve   # browse the lineage + docs
```

## Layout

| Path | What it is |
|---|---|
| `dbt_project.yml` | Project config (name, profile, materializations) |
| `models/sources.yml` | Raw tables dbt reads, never writes |
| `models/staging/` | `stg_saved_jobs` — URL cleansing, company normalization, title flags |
| `models/marts/` | Gold marts — drop-in replacements for the 3 hand-written views |
| `models/intermediate/` | Silver incremental models (`int_saved_jobs`, `int_job_skills`) |
| `snapshots/` | SCD-2 snapshot of saved_jobs status / apply_status |
| `tests/` | Singular tests — mart-vs-view parity + skills grain check |
| `profiles.yml.example` | Connection template — real file lives at `~/.dbt/profiles.yml` |

## Batch 6 — Silver + Gold + snapshot + schedule (2026-11-12)

**Silver (intermediate, incremental on `saved_at`):**
- `int_saved_jobs` — enriched job grain: all staging columns plus
  `fit_score_num` (numeric-guarded fit score) and
  `description_preview`, so the guard logic lives in one file.
- `int_job_skills` — one row per (job_id, skill); the JSON expansion
  from migration 005, done once here instead of on every read.
- Honest limit: saved_jobs has NO `updated_at` (verified live), so
  incremental runs see new rows only. In-place edits to old rows
  wait for the weekly full refresh; status history is the
  snapshot's job, not Silver's.

**Gold (marts — drop-in replacements for the 3 hand-written views):**
- `mart_job_analytics` ← `v_dashboard_job_analytics`
- `mart_market_metrics` ← `v_dashboard_market_metrics`
- `mart_job_skills` ← `v_dashboard_job_skills`
- Same columns, same order, same `status != 'DELETED'` filter.
  The dashboard (`src/protocols/dashboard_mcp.py`) can point at
  these without a single query change.
- Two deliberate decisions (both in the model headers):
  1. `mart_job_analytics.job_url` is `job_url_clean`, not the raw
     URL — the one value change, and the whole point of staging.
  2. `unique_hiring_companies` still counts raw `company` (parity
     with the view). Switching to `company_clean` merges case
     variants and changes a number the dashboard shows — one-line
     change for a later batch, not a silent change here.
- Parity is enforced by singular tests in `tests/`: row counts and
  job_id/skill set differences vs the live views. A failing parity
  test means the mart diverged — the run fails loudly.

**Snapshot:** `snap_saved_jobs_status` — SCD-2 (`check` strategy on
`status` + `apply_status`; no `updated_at` exists for a timestamp
strategy). First run snapshots all rows as version 1.
`outreach_tracking` gets NO snapshot: it is already CDC Type 2 by
design (the app versions it in `helper_repo.py`).

**Schedule (cron on de-lab, installed — see `crontab -l`):**
```cron
# Nightly 02:00 — build everything: models, snapshot, tests.
0 2 * * * bash -lc 'cd ~/de-job-intelligence/dbt && ~/venvs/dbt/bin/dbt build' >> ~/logs/dbt-nightly.log 2>&1
# Weekly full refresh, Sun 02:30 — catches in-place edits to old rows.
30 2 * * 0 bash -lc 'cd ~/de-job-intelligence/dbt && ~/venvs/dbt/bin/dbt build --full-refresh' >> ~/logs/dbt-nightly.log 2>&1
```
`bash -lc` loads `~/.bashrc`, so `DBT_PASSWORD` is available to the
run (cron itself does not read `.bashrc`). Logs go to
`~/logs/dbt-nightly.log`. The dashboard keeps serving the last good
marts if a run ever fails — nothing here touches the live views.

## What's next (same batch-wise rhythm)

1. **Point the dashboard at the marts** — swap the three
   `scout.v_dashboard_*` reads in `src/protocols/dashboard_mcp.py`
   to `analytics.mart_*` (or keep the views as the public surface
   and let the marts feed the demo graphs). His call.
2. **DuckDB weekly export** — Parquet export of jobs > 30 days old.
3. **FinOps widget** — "Cost per Application & Failover Telemetry"
   on `/engine-progress`.
