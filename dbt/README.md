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
| `models/marts/` | Gold marts (next batch — replace the 3 hand-written views) |
| `models/intermediate/` | Silver incremental models (next batch) |
| `snapshots/` | SCD-2 snapshots (next batch — outreach_tracking/job status) |
| `profiles.yml.example` | Connection template — real file lives at `~/.dbt/profiles.yml` |

## What's next (same batch-wise rhythm)

1. **Silver (intermediate):** incremental models on top of staging.
2. **Gold (marts):** rebuild `v_dashboard_job_analytics`,
   `v_dashboard_market_metrics`, `v_dashboard_job_skills` as dbt
   models with the EXACT same columns (the public MCP surface reads
   them — the contract is in `src/protocols/dashboard_mcp.py`).
3. **Snapshot:** SCD-2 on outreach_tracking / job status.
4. **Schedule:** a nightly run on de-lab (cron or systemd timer).
