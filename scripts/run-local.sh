#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if ! command -v node >/dev/null; then
  export PATH="$HOME/.local/share/course-tools/node-v22.16.0-linux-x64/bin:$PATH"
fi
[[ -x .venv/bin/python && -d frontend/node_modules ]] || { echo 'Run scripts/setup-local.sh first.'; exit 1; }
export DATABASE_URL=postgresql+psycopg://course_app:course_app@127.0.0.1:55432/courses
export PUBLIC_DATABASE_URL=postgresql+psycopg://course_public:course_public@127.0.0.1:55432/courses
export APP_ORIGIN=http://localhost:5173
# Keep the local Mailpit settings together when .env contains Gmail credentials.
export SMTP_HOST=127.0.0.1 SMTP_PORT=1025 SMTP_STARTTLS=false SMTP_USER= SMTP_PASSWORD=
export PYTHONPATH="$project_dir/backend"
pg_bin="${PG_BIN:-$(pg_config --bindir)}"
mkdir -p .runtime/pgsocket
if ! "$pg_bin/pg_ctl" -D "$project_dir/.runtime/pgdata" status >/dev/null; then
  "$pg_bin/pg_ctl" -D "$project_dir/.runtime/pgdata" -l "$project_dir/.runtime/postgres.log" -o "-p 55432 -h 127.0.0.1 -k $project_dir/.runtime/pgsocket" start
fi
local_pids=()
cleanup(){ for local_pid in "${local_pids[@]}"; do kill "$local_pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
if [[ -x .runtime/mailpit ]] && ! curl -fsS http://127.0.0.1:8025/api/v1/messages >/dev/null 2>&1; then
  .runtime/mailpit --smtp 127.0.0.1:1025 --listen 127.0.0.1:8025 --database .runtime/mailpit.db > .runtime/mailpit.log 2>&1 &
  local_pids+=("$!")
fi
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > .runtime/api.log 2>&1 &
local_pids+=("$!")
.venv/bin/python -m app.worker > .runtime/worker.log 2>&1 &
local_pids+=("$!")
(cd frontend && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort) &
local_pids+=("$!")
printf '\nFrontend: http://localhost:5173\nAPI: http://localhost:8000/docs\nMailpit: http://localhost:8025 (if installed)\nCtrl+C stops app processes; PostgreSQL data is retained.\n'
wait -n "${local_pids[@]}"
