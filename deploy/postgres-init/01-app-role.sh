#!/bin/sh
# Runs once, on first start of an empty data directory. Creates the least-privilege role the application connects as.
# It is deliberately not a superuser and has no BYPASSRLS, so row-level security applies to it (see 0001_init.sql).
set -eu
psql -v ON_ERROR_STOP=1 -v app_pw="$EVALRY_PG_APP_PASSWORD" --username postgres --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE evalry_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
SQL
