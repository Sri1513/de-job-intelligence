# pgvector image swap — backup / merge / verify runbook

Sprint 1, Batch 3. The compose file now uses `pgvector/pgvector:pg16`
instead of `postgres:16-alpine`. Same PostgreSQL major version (16),
same data-directory format — the `postgres_data` volume is reused
as-is, so no data migration is needed. The only change is the container
image (Debian-based instead of Alpine) plus the pgvector extension
becoming available.

## Why this order matters

Merging this PR triggers the CI deploy, and the deploy recreates the
postgres container with the new image. So the backup MUST happen
BEFORE the merge. After the merge there is no "before" anymore.

## Step 1 — Back up the live database (on the VM, before merging)

```bash
cd /opt/projects/de-job-intelligence
set -a; source .env; set +a
mkdir -p backups
docker compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc \
  > backups/job_scout_db-pre-pgvector-2026-11-12.dump
ls -lh backups/
file backups/job_scout_db-pre-pgvector-2026-11-12.dump
```

What each part does, so you learn it:

- `pg_dump -Fc` — dumps the whole database in PostgreSQL's custom
  format: compressed, and restorable with `pg_restore`. Plain SQL
  (`-Fp`, the default) would also work, but `-Fc` is smaller and
  restores faster. It runs INSIDE the container (`docker compose
  exec -T postgres ...`) because that is where the `pg_dump` binary
  and the database socket live — running `pg_dump` on the host fails
  the same way host `psql` did on 2026-11-01.
- `> backups/...` — the redirect happens on the HOST (outside the
  container), so the dump file lands in the repo checkout on the VM,
  not inside the container's filesystem. That is why the file must
  never be committed: `.gitignore` now excludes `backups/` and
  `*.dump`.
- `file ...` — must print `PostgreSQL custom database dump`. If it
  prints `empty` or `ASCII text` with an error, the dump failed —
  do NOT merge; read the error first.

## Step 2 — Merge the PR

Merge on GitHub as usual. CI runs the tests, then deploys: it syncs
`origin/main` and runs `docker compose up -d --build`, which pulls
`pgvector/pgvector:pg16` and recreates the `de-job-db` container
against the same `postgres_data` volume. The MCP server, dashboard,
and apply worker are also recreated (their images rebuild from the
new checkout), but their code is unchanged in this batch.

## Step 3 — Verify (on the VM, after the deploy)

```bash
cd /opt/projects/de-job-intelligence
set -a; source .env; set +a
docker compose ps postgres
docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
  -c "SELECT version();"
docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
  -c "SELECT count(*) FROM scout.saved_jobs;"
docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
  -c "SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector';"
```

Expected results:

- `docker compose ps` — `de-job-db` shows `Up` (not `Restarting`).
- `version()` — PostgreSQL 16.x, `on x86_64-pc-linux-gnu ... Debian`
  (the old image said `Alpine`). The `16` is what matters.
- `count(*)` — the same row count as before the swap (1300+ jobs;
  check it against the pre-swap number if you noted it).
- `pg_available_extensions` — one row: `vector | 0.8.x`. This proves
  the extension is AVAILABLE. It is not yet ENABLED — migration 008
  (Batch 4) runs `CREATE EXTENSION IF NOT EXISTS vector` itself.

## Rollback (only if something goes wrong)

The data lives in the `postgres_data` volume, which the swap never
touches — so the expected failure is a container that will not start,
not lost data. Roll back by reverting the image and recreating:

```bash
cd /opt/projects/de-job-intelligence
git revert <merge-commit>   # or: edit docker-compose.yml back to postgres:16-alpine
docker compose up -d postgres
docker compose ps postgres
```

Only if the data itself were damaged (not expected — the volume is
untouched) would you restore from the dump. The dump file lives on
the host, and `docker compose exec` runs inside the container, so
copy it in first — the container cannot see the host path directly:

```bash
docker cp backups/job_scout_db-pre-pgvector-2026-11-12.dump de-job-db:/tmp/restore.dump
docker compose exec -T postgres pg_restore -U "$DB_USER" -d "$DB_NAME" \
  -c /tmp/restore.dump
```
