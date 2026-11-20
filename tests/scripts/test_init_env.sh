#!/bin/sh
set -eu
cd -P "$(dirname "$0")/../.."
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
mkdir "$fixture/scripts"
cp scripts/init-env.sh "$fixture/scripts/"
cp .env.example "$fixture/"
cd "$fixture"

# Оба процесса должны завершиться с одним и тем же полностью записанным файлом.
sh scripts/init-env.sh > first.log 2>&1 &
first=$!
sh scripts/init-env.sh > second.log 2>&1 &
second=$!
wait "$first"
wait "$second"
test "$(stat -c '%a' .env)" = 600
grep -Eq '^POSTGRES_PASSWORD=[0-9a-f]{64}$' .env
sed 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=/' .env | cmp - .env.example
cp .env original.env
sh scripts/init-env.sh >> second.log
cmp original.env .env
if grep -Eq '[0-9a-f]{64}' first.log second.log; then
    printf '%s\n' 'Пароль попал в вывод.' >&2
    exit 1
fi
printf '%s\n' 'Создание .env, права доступа и повторный запуск проверены.'
