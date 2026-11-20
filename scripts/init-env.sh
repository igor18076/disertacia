#!/bin/sh
# Конфигурация первой установки. Существующий пароль может принадлежать действующему тому.
set -eu
cd -P "$(dirname "$0")/.."
if [ -f .env ]; then
    printf '%s\n' 'Используется существующий .env.'
    exit 0
fi
if [ -e .env ] || [ -L .env ]; then
    printf '%s\n' 'Проверьте путь .env в каталоге проекта.' >&2
    exit 1
fi

umask 077
temporary=$(mktemp .env.init.XXXXXXXX)
trap 'rm -f -- "$temporary"' EXIT
trap 'exit 1' HUP INT TERM
password=$(od -An -v -N32 -tx1 /dev/urandom | tr -d '[:space:]')
if [ "${#password}" -ne 64 ]; then
    printf '%s\n' 'Не удалось получить случайный пароль для PostgreSQL.' >&2
    exit 1
fi

found=0
while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = 'POSTGRES_PASSWORD=' ]; then
        printf 'POSTGRES_PASSWORD=%s\n' "$password"
        found=1
    else
        printf '%s\n' "$line"
    fi
done < .env.example > "$temporary"
if [ "$found" -ne 1 ]; then
    printf '%s\n' 'Проверьте поле POSTGRES_PASSWORD в .env.example.' >&2
    exit 1
fi

# Атомарная публикация защищает файл при двух одновременных запусках.
if ln -T -- "$temporary" .env; then
    printf '%s\n' 'Создан приватный .env с паролем PostgreSQL.'
elif [ -f .env ]; then
    printf '%s\n' 'Другой процесс уже создал .env.'
else
    exit 1
fi
