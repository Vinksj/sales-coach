#!/bin/sh
# End-to-end check of a cloud install in Docker (docs/deploy-cloud.md, "End-to-end check").
#   e2e/run.sh                 build, start, test, tear down (always): the container deploy (web, worker, schedulers)
#   e2e/run.sh vercel          the same checks against a simulation of Vercel's execution model: two read-only
#                              instances of the serverless entrypoint behind a round-robin edge, and a cron caller
#                              instead of worker and scheduler processes (e2e/docker-compose.vercel.yml,
#                              docs/deploy-vercel.md)
#   E2E_KEEP=1 e2e/run.sh      leave the stack running afterwards (tear down later with: e2e/run.sh [vercel] down)
# Needs Docker and a Python with the project's dependencies plus pytest (PYTHON, default: .venv/bin/python,
# else python3). Host ports 18140, 19000 and 15432 on 127.0.0.1 must be free.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
MODE=container
if [ "${1:-}" = "vercel" ]; then
    MODE=vercel
    shift
fi
if [ "$MODE" = "vercel" ]; then
    COMPOSE="docker compose -p sc-e2e-vercel -f $HERE/docker-compose.vercel.yml"
    IMAGE="sc-e2e-vercel-app:latest"
    SERVICES="db bootstrap fakes web proxy cron"
    BUILD_SERVICE=bootstrap
else
    COMPOSE="docker compose -p sc-e2e -f $HERE/docker-compose.yml"
    IMAGE="sc-e2e-app:latest"
    SERVICES="db migrate fakes web worker scheduler"
    BUILD_SERVICE=migrate
fi
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; else PYTHON=python3; fi
fi

teardown() {
    $COMPOSE down -v --remove-orphans --timeout 5 >/dev/null 2>&1
    docker image rm -f "$IMAGE" >/dev/null 2>&1
    echo "e2e ($MODE): torn down (containers, volumes, network, image)"
}

if [ "${1:-}" = "down" ]; then
    teardown
    exit 0
fi

collect_logs() {
    mkdir -p "$HERE/logs/$MODE"
    $COMPOSE ps -a > "$HERE/logs/$MODE/ps.txt" 2>&1
    for svc in $SERVICES; do
        $COMPOSE logs --no-color --timestamps "$svc" > "$HERE/logs/$MODE/$svc.log" 2>&1
    done
    echo "e2e ($MODE): logs in $HERE/logs/$MODE/"
}

finish() {
    status=$?
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ]; then collect_logs; fi
    if [ "${E2E_KEEP:-}" = "1" ]; then
        echo "e2e ($MODE): E2E_KEEP=1, the stack is still up; '$0 $( [ "$MODE" = vercel ] && echo 'vercel ')down' removes it"
    else
        teardown
    fi
    exit "$status"
}
trap finish EXIT INT TERM

$COMPOSE down -v --remove-orphans --timeout 5 >/dev/null 2>&1   # a clean start, always
$COMPOSE build "$BUILD_SERVICE" || exit 1
$COMPOSE up -d --wait --wait-timeout 240 || exit 1
cd "$REPO" || exit 1
E2E=1 E2E_MODE="$MODE" PYTHONPATH="$REPO" "$PYTHON" -m pytest -q -p no:cacheprovider -rA "$HERE/test_e2e.py" "$@"
