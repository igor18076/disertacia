#!/bin/sh
# Проверка выполняется на том же Linux-сервере, где будут запущены контейнеры.
set -u
issues=0
fail() { printf 'ОШИБКА: %s\n' "$1" >&2; issues=$((issues + 1)); }
info() { printf '%s\n' "$1"; }

if [ "$(uname -s)" != Linux ]; then
    info "Запустите проверку в оболочке целевого Linux-сервера."
    exit 1
fi
if [ "$#" -gt 1 ]; then
    info "Использование: sh scripts/preflight.sh [порт_API]"
    exit 2
fi
cd -P "$(dirname "$0")/.." || exit 1
info "Каталог проекта: $PWD"
info "Ядро и архитектура: $(uname -rm)"
info "Пользователь: $(id -un)"
if [ -r /etc/os-release ]; then
    sed -n 's/^PRETTY_NAME=//p' /etc/os-release
fi
info "Доступные логические CPU: $(nproc)"
awk '/^MemTotal:/ {printf "RAM всего: %.1f GiB\n", $2 / 1048576}
     /^MemAvailable:/ {printf "RAM доступно: %.1f GiB\n", $2 / 1048576}' /proc/meminfo
df -hP . || fail "Не удалось проверить диск проекта."
[ -w . ] && [ -x . ] || fail "Пользователю нужен доступ для обновления рабочей копии и настройки .env."
[ -r compose.yaml ] || fail "Файл compose.yaml недоступен."

# Читаем только строку порта. .env может содержать секреты и не является shell-скриптом.
env_port=
if [ -r .env ]; then
    env_port=$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?API_PORT[[:space:]]*=[[:space:]]*//p' .env | tail -n 1 |
        sed -E "s/[[:space:]]+#.*//; s/[[:space:]]+$//; s/^['\"]([0-9]+)['\"]$/\1/")
fi
port=${1:-${API_PORT:-${env_port:-8000}}}
case "$port" in
    ''|*[!0-9]*) info "Укажите порт API числом от 1 до 65535."; exit 2 ;;
esac
if ! [ "$port" -ge 1 ] 2>/dev/null || ! [ "$port" -le 65535 ] 2>/dev/null; then
    info "Допустимый диапазон порта API от 1 до 65535."
    exit 2
fi

published=
if command -v docker >/dev/null 2>&1; then
    docker --version
    endpoint=${DOCKER_HOST:-}
    if [ -n "${DOCKER_CONTEXT:-}" ] || [ -z "$endpoint" ]; then
        endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null) || endpoint=
    fi
    case "$endpoint" in
        ssh://*|tcp://*) info "Docker использует сетевое подключение. Сверьте, что демон расположен на проверяемом сервере." ;;
    esac
    if details=$(docker info --format '{{.OSType}} {{.ServerVersion}}' 2>/dev/null); then
        info "Docker Engine: $details"
        case "$details" in
            linux\ *)
                if ! dpkg --compare-versions "${details#linux }" ge 28.0.0; then
                    fail "Для сетевой изоляции порта на 127.0.0.1 нужен Docker Engine версии 28.0.0 или новее."
                fi ;;
            *) fail "Требуется Docker Engine для Linux-контейнеров." ;;
        esac
        data_root=$(docker info --format '{{.DockerRootDir}}')
        info "Хранилище Docker: $data_root"
        if [ -d "$data_root" ]; then
            df -hP "$data_root" || info "Администратору следует проверить место под тома Docker."
        else
            info "Проверьте с администратором свободное место на разделе хранилища Docker."
        fi
    else
        fail "Docker Engine недоступен текущему пользователю."
    fi
    if docker compose version; then
        if docker compose --profile test config --quiet >/dev/null 2>&1; then
            info "Compose принял конфигурацию проекта и тестового профиля."
            published=$(docker compose port api 8000 2>/dev/null) || published=
        else
            fail "Проверьте настройки .env и результат docker compose --profile test config --quiet."
        fi
    else
        fail "Плагин Docker Compose недоступен."
    fi
    docker buildx version || fail "Для сборки образа нужен плагин Buildx."
else
    fail "Команда docker отсутствует."
fi

if [ "$published" = "127.0.0.1:$port" ]; then
    info "TCP $port уже опубликован API этого проекта на 127.0.0.1."
elif command -v ss >/dev/null 2>&1 && listeners=$(ss -H -lnt "sport = :$port"); then
    if printf '%s\n' "$listeners" | awk '{print $4}' |
            grep -Eq "^(127\.0\.0\.1|0\.0\.0\.0|\*|\[::\]):$port$"; then
        fail "TCP $port занят. Согласуйте свободный API_PORT для .env."
    else
        info "На TCP $port конфликтующих слушающих сокетов не обнаружено."
    fi
else
    fail "Не удалось проверить порт через ss (пакет iproute2)."
fi

if [ "$issues" -gt 0 ]; then
    info "Обнаружено проблем: $issues."
    exit 1
fi
info "Предварительная проверка пройдена. Готовность сервисов подтвердит запуск тестов."
