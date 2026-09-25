#!/bin/sh
# End-to-end check of a cloud install in Docker (docs/deploy-cloud.md, "End-to-end check").
#   e2e/run.sh                 build, start, test, tear down (always)
#   E2E_KEEP=1 e2e/run.sh      leave the stack running afterwards (tear down later with: e2e/run.sh down)
# Needs Docker and a Python with the project's dependencies plus pytest (PYTHON, default: .venv/bin/python,
# else python3). Host ports 18140, 19000 and 15432 on 127.0.0.1 must be free.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
COMPOSE="docker compose -p sc-e2e -f $HERE/docker-compose.yml"
IMAGE="sc-e2e-app:latest"
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; else PYTHON=python3; fi
fi

teardown() {
    $COMPOSE down -v --remove-orphans --timeout 5 >/dev/null 2>&1
    docker image rm -f "$IMAGE" >/dev/null 2>&1
    echo "e2e: torn down (containers, volumes, network, image)"
}

if [ "${1:-}" = "down" ]; then
    teardown
    exit 0
fi

collect_logs() {
    mkdir -p "$HERE/logs"
    $COMPOSE ps -a > "$HERE/logs/ps.txt" 2>&1
    for svc in db migrate fakes web worker scheduler; do
        $COMPOSE logs --no-color --timestamps "$svc" > "$HERE/logs/$svc.log" 2>&1
    done
    echo "e2e: logs in $HERE/logs/"
}

finish() {
    status=$?
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ]; then collect_logs; fi
    if [ "${E2E_KEEP:-}" = "1" ]; then
        echo "e2e: E2E_KEEP=1, the stack is still up; '$0 down' removes it"
    else
        teardown
    fi
    exit "$status"
}
trap finish EXIT INT TERM

$COMPOSE down -v --remove-orphans --timeout 5 >/dev/null 2>&1   # a clean start, always
$COMPOSE build migrate || exit 1
$COMPOSE up -d --wait --wait-timeout 240 || exit 1
cd "$REPO" || exit 1
E2E=1 PYTHONPATH="$REPO" "$PYTHON" -m pytest -q -p no:cacheprovider -rA "$HERE/test_e2e.py" "$@"
