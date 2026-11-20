#!/bin/sh
# Приёмка инфраструктуры из рабочей копии на Debian.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s\n' 'Использование: sh scripts/accept-infra.sh' >&2
    exit 2
fi
cd -P "$(dirname "$0")/.."
umask 077
directory="$PWD/results/infrastructure"
mkdir -p "$directory"
started=$(date -u +%Y%m%dT%H%M%SZ)
report=$(mktemp --suffix=.txt "$directory/$started-XXXXXX")
printf 'Отчёт приёмки: %s\n' "$report"

revision=$(git rev-parse --verify HEAD 2>/dev/null) || revision=unknown
worktree=unknown
if changes=$(git status --porcelain 2>/dev/null); then
    if [ -n "$changes" ]; then worktree=modified; else worktree=clean; fi
fi
printf 'started_utc=%s\ngit_commit=%s\ngit_worktree=%s\n' "$started" "$revision" "$worktree" > "$report"

step=env
fail_step() {
    printf 'FAIL %s (%s)\nresult=FAIL\n' "$step" "$1" >> "$report"
    printf 'Приёмка остановлена на шаге %s, код %s. Отчёт: %s\n' "$step" "$1" "$report" >&2
    exit "$1"
}
trap 'fail_step 129' HUP
trap 'fail_step 130' INT
trap 'fail_step 143' TERM

run_step() {
    step=$1
    shift
    printf '\nШаг %s\n' "$step"
    "$@" || fail_step "$?"
    printf 'PASS %s\n' "$step" >> "$report"
}

check_migration() {
    container=$(docker compose ps --all --quiet migrate) || return 1
    if [ -z "$container" ]; then
        printf '%s\n' 'Контейнер migrate отсутствует.' >&2
        return 1
    fi
    state=$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$container") || return 1
    if [ "$state" != 'exited 0' ]; then
        printf 'Состояние migrate: %s. Подробности: docker compose logs migrate\n' "$state" >&2
        return 1
    fi
    printf 'migrate_exit=0\n' >> "$report"
}

check_readiness() {
    binding=$(docker compose port api 8000) || return 1
    port=${binding#127.0.0.1:}
    case "$port" in
        ''|*[!0-9]*) printf '%s\n' 'Ожидается единственная публикация API на 127.0.0.1.' >&2; return 1 ;;
    esac
    if [ "$binding" != "127.0.0.1:$port" ] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        printf '%s\n' 'Некорректный опубликованный порт API.' >&2
        return 1
    fi

    docker compose exec -T api python -c '
import json
import os
from http.client import HTTPConnection

connection = HTTPConnection("127.0.0.1", 8000, timeout=int(os.environ["PROBE_TIMEOUT_SECONDS"]) + 5)
connection.request("GET", "/ready")
response = connection.getresponse()
if response.status != 200:
    raise SystemExit(f"/ready: HTTP {response.status}")
body = json.load(response)
checks = body.get("checks", {})
if body.get("status") != "ready" or any(checks.get(service) != "ok" for service in ("postgres", "qdrant")):
    raise SystemExit("PostgreSQL или Qdrant не готовы")
' || return 1
    printf 'api_binding=%s\nready_http=200\npostgres=ok\nqdrant=ok\n' "$binding" >> "$report"
}

run_step env sh scripts/init-env.sh
run_step preflight sh scripts/preflight.sh
run_step stack docker compose up -d --build --wait
run_step migrate check_migration
run_step ready check_readiness
run_step tests docker compose --profile test run --rm --build tests

printf 'result=PASS\n' >> "$report"
trap - HUP INT TERM
printf '\nПриёмка завершена успешно. Отчёт: %s\n' "$report"
