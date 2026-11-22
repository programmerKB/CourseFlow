#!/usr/bin/env bash
set -euo pipefail
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set app_password="$APP_DB_PASSWORD" --set public_password="$PUBLIC_DB_PASSWORD" -v ON_ERROR_STOP=1 <<'SQL'
SELECT format('CREATE ROLE course_app LOGIN PASSWORD %L', :'app_password') \gexec
SELECT format('CREATE ROLE course_public LOGIN PASSWORD %L', :'public_password') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
