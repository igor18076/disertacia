# Эксплуатация на Debian

Для клонирования, запуска и остановки используйте команды в [README](../README.md). Путь `/opt/coursegen` предполагает, что администратор выделил его для проекта и дал оператору право записи. Подойдёт и другой разрешённый каталог. Все команды ниже выполняются в корне рабочей копии на сервере.

## Проверка сервера

Для полного прогона используйте `sh scripts/accept-infra.sh` из README. Для отдельной проверки окружения подготовьте `.env` командой `sh scripts/init-env.sh`, затем выполните следующую команду от пользователя, который будет работать с контейнерами. Docker CLI должен обращаться к локальному демону этого же сервера.

```sh
sh scripts/preflight.sh
```

Скрипт выводит версию ОС и ядра, доступные CPU и RAM, свободное место в каталоге проекта, путь хранилища Docker, версии Engine, Compose и Buildx. Он проверяет права пользователя, конфигурацию Compose и TCP-порт API. По умолчанию это 8000. Для порта, заданного сложным выражением в `.env`, передайте итоговое число явно, например `sh scripts/preflight.sh 18000`. Запуск выполняет только операции чтения.

Код возврата 0 означает, что предварительные проверки прошли. При коде 1 найдите строку с пометкой «ОШИБКА», при коде 2 проверьте аргумент порта. Порт, уже опубликованный API текущего проекта, допустим при повторной проверке. Место для именованных томов выделяется на разделе хранилища Docker. Если оператору недоступен этот путь, количество свободного места проверяет администратор. Сопоставьте свободное место и RAM с объёмом предстоящего эксперимента.

### Если Docker отсутствует

На Forky/Sid администратор может использовать пакеты той ветки Debian, которая уже настроена на сервере. Для оценки состава изменений выполните следующие команды. [Пакеты Debian Forky](https://packages.debian.org/search?suite=forky&searchon=names&keywords=docker)

```sh
apt-cache policy docker.io docker-cli docker-buildx docker-compose
apt-get -s install docker.io docker-cli docker-buildx docker-compose
```

После согласования состава изменений администратор может установить их командой `sudo apt-get install docker.io docker-cli docker-buildx docker-compose`, проверить доступ пользователя к демону и повторить проверку. Установка Docker может менять сетевые правила хоста. Если на сервере уже есть Docker CE, обновление выполняют из его согласованного источника. Docker публикует перечень поддерживаемых выпусков Debian и отдельные указания для Debian testing. [Инструкция Docker Engine](https://docs.docker.com/engine/install/debian/)

## Сеть и диагностика

API опубликован на IPv4-адресе `127.0.0.1`. Подключайтесь с Windows через SSH-туннель из README. PostgreSQL и Qdrant доступны контейнерам во внутренней сети Compose. Для изоляции публикации на loopback используйте Engine 28.0.0 или новее. Docker описывает доступ с соседних узлов одного сегмента в более ранних версиях. [Публикация портов Docker](https://docs.docker.com/engine/network/port-publishing/)

```sh
docker compose ps -a
docker compose logs --tail 100 migrate api postgres qdrant
docker compose port api 8000
```

Последняя команда должна показать `127.0.0.1` и порт из `.env`. В рамках этого развёртывания оператор управляет контейнерами и данными проекта. Изменение правил сети, системных служб, дисков или прав доступа согласовывается отдельно.

### Ошибка DNS при загрузке пакетов Python

Если шаг `stack` завершился на `RUN uv sync` с сообщением `failed to lookup address information`, проверьте разрешение имён на самом Debian. Затем повторите сборку API. При успешной сборке можно сразу повторить приёмку.

```sh
getent ahostsv4 pypi.org
getent ahostsv4 files.pythonhosted.org
docker compose --progress plain build api && sh scripts/accept-infra.sh
```

Если `getent` вернул адреса для обоих имён, а сборщик снова сообщил об ошибке DNS, повторите приёмку с дополнительным файлом Compose. Команда предназначена для штатного набора файлов проекта. Если используете другие Compose-файлы, включите их в тот же список в привычном порядке перед `compose.build-dns.yaml`.

```sh
PYPI_INDEX_IPV4="$(getent ahostsv4 pypi.org | awk 'NR == 1 {print $1}')" \
PYPI_FILES_IPV4="$(getent ahostsv4 files.pythonhosted.org | awk 'NR == 1 {print $1}')" \
COMPOSE_FILE=compose.yaml:compose.build-dns.yaml sh scripts/accept-infra.sh
```

Адреса вычисляются заново для каждого такого запуска. Параметр `build.extra_hosts` действует при сборке образов API, worker, миграции и тестов. HTTPS использует исходные имена узлов, а uv проверяет пакеты по `uv.lock`. [Документация Docker](https://docs.docker.com/reference/compose-file/build/#extra_hosts)

Если на самом Debian `getent` не выдаёт адрес, проверьте доступ сервера к настроенному DNS-резолверу. При тайм-ауте TCP после успешного разрешения имён можно запустить сборку с локальными пакетами по инструкции ниже.

### Сборка с локальными пакетами Python

При ошибке `tcp connect error: deadline has elapsed` на `files.pythonhosted.org` пакеты для Debian amd64 можно перенести с Windows. Архив `dist/coursegen-wheels-cp312-linux-amd64.tar.gz` содержит пакеты для CPython 3.12, включая зависимости тестов. Сборка берёт требования и хеши из `uv.lock` в текущей версии проекта. Базовые образы Python, uv, PostgreSQL и Qdrant по-прежнему должны быть доступны Docker через реестры или локальный кеш.

**На Windows.** Для этапа 3 передайте архив wheels из текущего lock-файла и архив модели. Когда оба файла уже есть в `dist/`, достаточно команды `scp`. Для повторной подготовки используйте Python 3.12 и uv 0.13.9 из корня проекта.

```powershell
uv run --locked --group test python scripts/prepare_wheelhouse.py
py -3.12 scripts/prepare_search_model.py
scp .\dist\coursegen-wheels-cp312-linux-amd64.tar.gz .\dist\coursegen-search-model.tar.gz user@server:~/
```

Подставьте в `user@server` тот же адрес и логин, с которыми вы заходите по SSH. Обновите код проекта в Git обычным способом. Архив передаётся отдельно.

**На Debian.** Из каталога проекта обновите код, распакуйте архив и запустите приёмку. Для установки в другом каталоге замените путь в первой команде.

```sh
cd /var/www/my-project
git pull --ff-only &&
  tar -xzf ~/coursegen-wheels-cp312-linux-amd64.tar.gz &&
  tar -xzf ~/coursegen-search-model.tar.gz &&
  COMPOSE_FILE=compose.yaml:compose.wheels.yaml sh scripts/accept-infra.sh
```

В этом режиме BuildKit передаёт каталог `wheelhouse/` отдельным контекстом и отключает сеть для команд сборки. Отсутствие нужного пакета или несовпадение SHA-256 прерывает установку. После изменения `uv.lock` подготовьте архив заново. [Именованные контексты Docker Compose](https://docs.docker.com/reference/compose-file/build/#additional_contexts)

## Резервная копия

Перед обновлением дождитесь завершения операций записи. Следующие команды создают каталог с закрытыми правами в `backups/`, логический архив PostgreSQL и полный снимок одноузлового Qdrant. Оба сервиса должны работать. Снимок Qdrant запрашивается через внутреннюю сеть из контейнера API. [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) · [Снимки Qdrant](https://qdrant.tech/documentation/operations/snapshots/)

```sh
(
  set -eu
  umask 077
  mkdir -p backups
  backup=$(mktemp -d "backups/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
  docker compose exec -T postgres sh -ec 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$backup/postgresql.dump"
  docker compose exec -T postgres pg_restore --list < "$backup/postgresql.dump" > /dev/null
  snapshot=$(docker compose exec -T api python -c 'import os; from qdrant_client import QdrantClient; print(QdrantClient(url=os.environ["QDRANT_URL"], timeout=300).create_full_snapshot().name)')
  docker compose cp "qdrant:/qdrant/snapshots/$snapshot" "$backup/$snapshot"
  git rev-parse HEAD > "$backup/commit"
  (cd "$backup" && sha256sum postgresql.dump "$snapshot" > SHA256SUMS)
  printf 'Копия готова: %s\n' "$backup"
)
```

Перенесите готовый каталог на согласованный носитель. Восстановление проверяйте в отдельном Compose-проекте на свободном порту с новыми томами и версиями образов из сохранённого коммита. Проверьте `sha256sum -c SHA256SUMS` в каталоге копии, загрузите дамп через `pg_restore` в пустую БД и передайте файл снимка одноузловому Qdrant при старте параметром `--storage-snapshot`. После запуска API проверьте readiness и интеграционные тесты по README. Конфигурацию с секретами храните в защищённом хранилище, отдельно от Git.
