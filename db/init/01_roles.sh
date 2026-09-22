#!/usr/bin/env bash
# Creates the two application roles and the dev/test databases.
#
#   app_owner  owns the schema; runs Alembic migrations. Has CREATEDB so the
#              test suite can create scratch databases for migration up/down.
#   app_rw     the application role. Not the table owner, NOSUPERUSER,
#              NOBYPASSRLS, so FORCE ROW LEVEL SECURITY applies to it.
#
# Runs automatically inside the postgres container on first start
# (docker-entrypoint-initdb.d). Also runnable against any Postgres with the
# usual PG* environment variables (used by CI):
#   PGHOST=localhost PGPASSWORD=postgres db/init/01_roles.sh
# PG_MAINTENANCE_DB is the database the role and CREATE DATABASE statements connect
# to: "postgres" (default; Docker, CI) or "defaultdb" on DigitalOcean Managed Postgres,
# which has no "postgres" database (docs/OPERATIONS.md, "Production").
set -euo pipefail

OWNER_PW="${APP_OWNER_PASSWORD:-app_owner_dev}"
RW_PW="${APP_RW_PASSWORD:-app_rw_dev}"
DATABASES="${APP_DATABASES:-wip wip_test}"
MAINT_DB="${PG_MAINTENANCE_DB:-postgres}"
PSQL=(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}")

"${PSQL[@]}" --dbname "${MAINT_DB}" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_owner') THEN
    CREATE ROLE app_owner LOGIN PASSWORD '${OWNER_PW}' CREATEDB NOSUPERUSER NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
    CREATE ROLE app_rw LOGIN PASSWORD '${RW_PW}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
\$\$;
SQL

for db in ${DATABASES}; do
  exists=$("${PSQL[@]}" --dbname "${MAINT_DB}" -tAc "SELECT 1 FROM pg_database WHERE datname = '${db}'")
  if [ "${exists}" != "1" ]; then
    "${PSQL[@]}" --dbname "${MAINT_DB}" -c "CREATE DATABASE ${db} OWNER app_owner"
  fi
  # In Postgres 15+ the public schema is owned by the database owner (app_owner).
  # app_rw gets DML on everything app_owner creates, and nothing else.
  "${PSQL[@]}" --dbname "${db}" <<SQL
GRANT USAGE ON SCHEMA public TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
SQL
done
