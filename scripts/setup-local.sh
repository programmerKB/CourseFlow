#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if ! command -v node >/dev/null; then
  export PATH="$HOME/.local/share/course-tools/node-v22.16.0-linux-x64/bin:$PATH"
fi
command -v node >/dev/null || { echo 'Node.js 22 is required.'; exit 1; }
pg_bin="${PG_BIN:-$(pg_config --bindir)}"
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
mkdir -p .runtime/pgsocket
if [[ ! -f .runtime/pgdata/PG_VERSION ]]; then
  "$pg_bin/initdb" -D "$project_dir/.runtime/pgdata" -A trust -U course_migrator
fi
if ! "$pg_bin/pg_ctl" -D "$project_dir/.runtime/pgdata" status >/dev/null; then
  "$pg_bin/pg_ctl" -D "$project_dir/.runtime/pgdata" -l "$project_dir/.runtime/postgres.log" -o "-p 55432 -h 127.0.0.1 -k $project_dir/.runtime/pgsocket" start
fi
psql -h 127.0.0.1 -p 55432 -U course_migrator -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE ROLE course_app LOGIN PASSWORD ''course_app''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='course_app') \gexec
SELECT 'CREATE ROLE course_public LOGIN PASSWORD ''course_public''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='course_public') \gexec
SELECT 'CREATE DATABASE courses' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='courses') \gexec
SQL
export DATABASE_URL=postgresql+psycopg://course_migrator@127.0.0.1:55432/courses
(cd backend && ../.venv/bin/python -m alembic upgrade head)
export DATABASE_URL=postgresql+psycopg://course_app:course_app@127.0.0.1:55432/courses
PYTHONPATH=backend .venv/bin/python -m app.seed --scenario "${1:-add-drop}"
printf '\nSetup completed. Run scripts/run-local.sh\n'
