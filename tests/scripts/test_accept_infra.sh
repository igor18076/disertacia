#!/bin/sh
# Проверка оркестрации без Docker. Для теста нужны sh, Git и Python 3.
set -eu
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin" "$scratch/checkout/scripts"
cd "$scratch/checkout"
cp "$repo/scripts/accept-infra.sh" scripts/
git init -q
git -c user.name=Fixture -c user.email=fixture@example.invalid commit --allow-empty -qm fixture
commit=$(git rev-parse HEAD)

cat > scripts/init-env.sh <<'SH'
echo env >> "$TRACE"
[ "$SCENARIO" != env ]
SH
cat > scripts/preflight.sh <<'SH'
echo preflight >> "$TRACE"
[ "$SCENARIO" != preflight ]
SH
# Выполняем переданный контейнеру Python-код с контролируемым ответом HTTP.
cat > "$scratch/ready.py" <<'PY'
import json
import os
import sys
from unittest.mock import patch

with patch("http.client.HTTPConnection") as connection:
    response = connection.return_value.getresponse.return_value
    mode = os.environ["SCENARIO"]
    response.status = 503 if mode == "http_503" else 200
    checks = {"postgres": "ok", "qdrant": "unavailable" if mode == "dependency" else "ok"}
    response.read.return_value = json.dumps({"status": "ready", "checks": checks}).encode()
    exec(sys.argv[1], {})
    connection.return_value.request.assert_called_once_with("GET", "/ready")
PY
cat > "$scratch/bin/docker" <<'SH'
#!/bin/sh
set -eu
case "$*" in
    'compose up -d --build --wait')
        echo stack >> "$TRACE"
        cat .env
        [ "$SCENARIO" != stack ] ;;
    'compose ps '*migrate)
        echo migrate-id >> "$TRACE"
        echo id-migrate ;;
    'inspect '*id-migrate)
        echo inspect >> "$TRACE"
        case "$SCENARIO" in
            migrate) echo 'exited 1' ;;
            running) echo 'running 0' ;;
            *)       echo 'exited 0' ;;
        esac ;;
    'compose port api 8000')
        echo port >> "$TRACE"
        case "$SCENARIO" in
            public) echo '0.0.0.0:18000' ;;
            *)      echo '127.0.0.1:18000' ;;
        esac ;;
    'compose exec -T api python -c '*)
        echo ready >> "$TRACE"
        PROBE_TIMEOUT_SECONDS=3 python3 "$STUB_READY" "$7" ;;
    'compose --profile test run --rm --build tests')
        echo tests >> "$TRACE"
        if [ "$SCENARIO" = tests ]; then exit 5; fi ;;
    *)
        printf 'unexpected docker %s\n' "$*" >> "$TRACE"
        exit 97 ;;
esac
SH
chmod +x "$scratch/bin/docker"
PATH="$scratch/bin:$PATH"
STUB_READY="$scratch/ready.py"
export PATH STUB_READY
printf '%s\n' 'POSTGRES_PASSWORD=only-for-testing-report-redaction' > .env
cp .env "$scratch/original.env"

fail() { echo "FAIL: $*" >&2; exit 1; }
run_case() {
    SCENARIO=$1
    expected_step=$2
    expected_trace=$3
    TRACE="$scratch/trace"
    export SCENARIO TRACE
    : > "$TRACE"
    status=0
    sh scripts/accept-infra.sh > "$scratch/output" 2>&1 || status=$?
    if [ "$expected_step" = success ]; then
        [ "$status" -eq 0 ] || { cat "$scratch/output" >&2; fail "$SCENARIO ($status)"; }
    else
        [ "$status" -ne 0 ] || fail "$SCENARIO: скрипт вернул 0"
    fi
    actual_trace=$(paste -sd ' ' "$TRACE")
    [ "$actual_trace" = "$expected_trace" ] || fail "$SCENARIO: $actual_trace"
    # У каждого запуска должен появиться отчёт, включая аварийные запуски.
    set -- results/infrastructure/*.txt
    [ "$#" -eq 1 ] && [ -f "$1" ] || fail "$SCENARIO: ожидался один отчёт"
    report=$1
    grep -Fxq "git_commit=$commit" "$report" || fail "$SCENARIO: commit"
    grep -Fxq 'git_worktree=modified' "$report" || fail "$SCENARIO: незакоммиченные изменения"
    if grep -Fq 'only-for-testing-report-redaction' "$report"; then
        fail "$SCENARIO: пароль попал в отчёт"
    fi
    cmp -s .env "$scratch/original.env" || fail "$SCENARIO: изменён .env"
    if [ "$expected_step" = success ]; then
        grep -Fxq 'result=PASS' "$report" || fail "$SCENARIO: result"
        for step in env preflight stack migrate ready tests; do
            grep -Fxq "PASS $step" "$report" || fail "$SCENARIO: $step"
        done
    else
        grep -Fxq 'result=FAIL' "$report" || fail "$SCENARIO: result"
        grep -q "^FAIL $expected_step " "$report" || fail "$SCENARIO: $expected_step"
        if [ "$SCENARIO" = tests ]; then
            [ "$status" -eq 5 ] && grep -Fxq 'FAIL tests (5)' "$report" || fail "потерян код pytest"
        fi
    fi
    mv "$report" "$scratch/$SCENARIO.txt"
}

base='env preflight stack'
migrated="$base migrate-id inspect"
ready="$migrated port ready"
run_case success success "$ready tests"
run_case env env 'env'
run_case preflight preflight 'env preflight'
run_case stack stack "$base"
run_case migrate migrate "$migrated"
run_case running migrate "$migrated"
run_case public ready "$migrated port"
run_case http_503 ready "$ready"
run_case dependency ready "$ready"
run_case tests tests "$ready tests"
echo 'OK: пройдено 10 сценариев приёмочного скрипта'
