#!/usr/bin/env bash
# deploy/deploy.sh [ref] — deploy a release on the jobcost.dev droplet (F05.0, D-27).
#
# Run as root on the droplet:  bash /opt/wip/deploy/deploy.sh            (origin/main)
#                              bash /opt/wip/deploy/deploy.sh <sha|ref>  (a release, or a roll-back)
#
# Order, and why (plan question 2): lock; record what is serving; fetch and check out
# the ref, detached; pip; npm ci and a Vite build into dist.new (the served dist is
# untouched); `alembic upgrade head` as root from /etc/wip/migrate.env BEFORE any
# restart, so a failed migration leaves the old release serving and exits here; swap
# the frontend; restart wip-api and wip-worker; health check through nginx. Every step
# before the restart is safe to re-run. No automatic roll-back: migrations are
# forward-only in production; the failure message says the roll-back command.
# Never prints a secret; the migrate.env variables are exported around alembic only.
set -euo pipefail

exec 9>/run/lock/wip-deploy.lock
flock -n 9 || { echo "another deploy is running (lock /run/lock/wip-deploy.lock)"; exit 1; }

REF="${1:-origin/main}"
APP_USER=wip
APP_DIR=/opt/wip
MIGRATE_ENV="${MIGRATE_ENV_FILE:-/etc/wip/migrate.env}"   # override only for the deploy-check
HEALTH_URL=https://jobcost.dev/api/health
VENV="$APP_DIR/backend/.venv"

[ "$(id -u)" = 0 ] || { echo "deploy.sh: run as root" >&2; exit 1; }
[ -r "$MIGRATE_ENV" ] || { echo "deploy.sh: $MIGRATE_ENV is missing" >&2; exit 1; }

as_wip() { sudo -u "$APP_USER" -H "$@"; }
# Every git command runs as wip, the checkout's owner: root would otherwise refuse the
# repository as "dubious ownership" (found on the first deploy, 2026-09-22), and root
# needs no safe.directory entry.
git_wip() { as_wip git -C "$APP_DIR" "$@"; }

STEP=start
PREVIOUS="$(git_wip rev-parse HEAD)"
on_error() {
    echo >&2
    echo "deploy failed at step: $STEP" >&2
    echo "was serving: $PREVIOUS (still serving unless the step was 'restart' or 'health')" >&2
    echo "to go back:  bash $APP_DIR/deploy/deploy.sh $PREVIOUS" >&2
}
trap on_error ERR

echo "== deploy $REF (serving $PREVIOUS) =="
free -m

STEP=fetch
git_wip fetch --prune --quiet origin

STEP=checkout
git_wip checkout --detach --quiet "$REF"
SHA="$(git_wip rev-parse HEAD)"
echo "checked out $SHA"

STEP="node version"
WANT="$(tr -d '[:space:]' < "$APP_DIR/frontend/.nvmrc")"
[[ "$(node --version)" == "v${WANT}."* ]] || {
    echo "node is $(node --version); frontend/.nvmrc wants $WANT (run setup.sh)" >&2
    false
}

STEP=pip
as_wip "$VENV/bin/pip" install --quiet -e "$APP_DIR/backend"

STEP="npm ci"
as_wip env --chdir="$APP_DIR/frontend" npm ci --no-audit --no-fund --loglevel=error

STEP="frontend build"
as_wip env --chdir="$APP_DIR/frontend" npx vite build --outDir dist.new --emptyOutDir --logLevel warn
free -m

STEP=migrate
# Exported around alembic only: DATABASE_OWNER_URL (app_owner) and PROTECTED_DATABASE_NAMES.
# Root runs it; PYTHONDONTWRITEBYTECODE keeps root-owned .pyc files out of wip's tree.
(
    set -a
    # shellcheck disable=SC1090
    . "$MIGRATE_ENV"
    set +a
    cd "$APP_DIR/backend"
    ENV_FILE="$MIGRATE_ENV" PYTHONDONTWRITEBYTECODE=1 timeout 600 "$VENV/bin/alembic" upgrade head
)

STEP="frontend swap"
as_wip env --chdir="$APP_DIR/frontend" sh -c 'rm -rf dist.prev; [ -d dist ] && mv dist dist.prev; mv dist.new dist'

STEP=restart
systemctl restart wip-api wip-worker

STEP=health
# Up to ten tries, two seconds apart. "No answer" (nginx or the API not up) is told apart
# from a 503 whose body says the API is up but its database connection failed (first
# deploy, 2026-09-22: a DATABASE_URL password that did not match the role).
ok=0
code=""
body=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    body="$(curl -sS --max-time 5 -w '\n%{http_code}' "$HEALTH_URL" 2>/dev/null || true)"
    code="${body##*$'\n'}"
    body="${body%$'\n'*}"
    if [ "$code" = 200 ] && [[ "$body" == *'"status":"ok"'* && "$body" == *'"db":"ok"'* ]]; then
        ok=1
        break
    fi
    sleep 2
done
if [ "$ok" != 1 ]; then
    if [[ "$body" == *'"db":"unavailable"'* ]]; then
        echo "health check failed (HTTP $code): the API is up but its database connection failed." >&2
        echo "check DATABASE_URL in /etc/wip/app.env: host, port, sslmode=require, and the app_rw" >&2
        echo "password, which must equal the role's (ALTER ROLE app_rw PASSWORD '…' as doadmin if not);" >&2
        echo "then: systemctl restart wip-api wip-worker" >&2
    elif [ -z "$code" ] || [ "$code" = 000 ]; then
        echo "health check failed: no answer from $HEALTH_URL within 20 s (nginx or wip-api not up)" >&2
    else
        echo "health check failed: $HEALTH_URL answered HTTP $code without status ok / db ok" >&2
    fi
    echo "the services were restarted on $SHA; read: journalctl -u wip-api -u wip-worker -n 100" >&2
    false
fi

echo "deployed $SHA"
free -m
