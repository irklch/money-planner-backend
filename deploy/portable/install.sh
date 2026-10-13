#!/usr/bin/env bash
# Установка на чистый сервер (Ubuntu 22.04/24.04, Debian 12; x86_64 или arm64; от 1 vCPU / 2 ГБ).
# Запуск из клона репозитория: sudo deploy/portable/install.sh [--test] [--prepare]
# Идемпотентен: повторный запуск обновляет образы и перезапускает сервисы.
#
# --test     тестовый стенд без домена: Caddy не запускается, снаружи открыт только SSH,
#            API — на 127.0.0.1:8000 (доступ через ssh -L). Требует FENCE_MODE=off. Не для production.
# --prepare  подготовка к восстановлению (docs/disaster-recovery.md §3): Docker, проверки, сборка образов
#            и только пустая БД. Миграции, API, maintenance, backup и Caddy не запускаются. Затем —
#            backup restore, затем этот же скрипт без --prepare (миграции догонят схему, сервисы стартуют).
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "install: $*" >&2; exit 1; }

test_mode=false
prepare=false
for arg in "$@"; do
	case "$arg" in
	--test) test_mode=true ;;
	--prepare) prepare=true ;;
	*) die "usage: install.sh [--test] [--prepare]" ;;
	esac
done

[ "$(id -u)" = 0 ] || die "run as root (sudo)"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
	# Пакеты дистрибутива: без внешних скриптов и без регистраций у Docker Inc.
	apt-get update -q
	apt-get install -y -q docker.io docker-compose-v2 || apt-get install -y -q docker.io docker-compose-plugin
	systemctl enable --now docker
fi

[ -f .env ] || die "create deploy/portable/.env from env.example first (chmod 600)"
chmod 600 .env
# .env читается построчно как KEY=VALUE, без выполнения shell-кода: иначе & ~ * ? в паролях
# интерпретировались бы оболочкой. Формат совпадает с тем, как .env читает Docker Compose.
load_env() {
	local line key value
	while IFS= read -r line || [ -n "$line" ]; do
		line=${line%$'\r'}
		case "$line" in '' | '#'*) continue ;; esac
		[[ $line == *=* ]] || die ".env: not KEY=VALUE: ${line%%=*}"
		key=${line%%=*}
		value=${line#*=}
		[[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die ".env: bad key: $key"
		if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then value=${BASH_REMATCH[1]}; fi
		export "$key=$value"
	done <.env
}
load_env
for v in API_DOMAIN FENCE_EXPECTED_IP INSTANCE_NAME POSTGRES_PASSWORD JWT_SECRET TOKEN_DERIVATION_SECRET BACKUP_AGE_RECIPIENT; do
	[ -n "${!v:-}" ] || die "$v is empty in .env"
done
[ "$JWT_SECRET" != "$TOKEN_DERIVATION_SECRET" ] || die "JWT_SECRET and TOKEN_DERIVATION_SECRET must differ"
# Пароль БД в URL кодирует приложение; здесь — только символы, которые ломают разбор .env и Compose.
check_password() {
	local name=$1 value=$2
	[ "${#value}" -ge 24 ] || die "$name must be at least 24 characters (openssl rand -hex 32)"
	case "$value" in
	*[[:space:]\$\"\'\`\\#]*) die "$name must not contain whitespace, \$, quotes, backslash or # (openssl rand -hex 32)" ;;
	esac
}
check_password POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
if $test_mode; then
	[ "${FENCE_MODE:-}" = off ] || die "--test requires FENCE_MODE=off (no domain points to a test server)"
	[ -z "${COMPOSE_PROFILES:-}" ] || die "--test requires empty COMPOSE_PROFILES (Caddy must not run without a domain)"
	services=(db migrate api maintenance backup)
else
	[ "${COMPOSE_PROFILES:-}" = public ] || die "set COMPOSE_PROFILES=public in .env (Caddy, HTTPS)"
	[ "${FENCE_MODE:-enforce}" = enforce ] || echo "install: WARNING: FENCE_MODE=$FENCE_MODE, the startup DNS check is off" >&2
	services=()
fi
[ -n "${BACKUP_REMOTE:-}" ] || echo "install: WARNING: BACKUP_REMOTE is empty, backups will stay on this server only" >&2

# Брандмауэр: снаружи только SSH и HTTP(S) (в --test — только SSH). Порт БД compose не публикует.
if command -v ufw >/dev/null; then
	ufw allow OpenSSH >/dev/null
	$test_mode || { ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null; }
	ufw --force enable >/dev/null
fi

IMAGE_TAG=$(git -C ../.. rev-parse --short HEAD 2>/dev/null || echo local)
export IMAGE_TAG
docker compose build
if $prepare; then
	if [ -n "$(docker compose ps -q --status running migrate api maintenance backup 2>/dev/null)" ]; then
		die "--prepare is for a new server: application services are already running here"
	fi
	docker compose up -d --wait db
	tables=$(docker compose exec -T db psql -U money -d money -At -c \
		"select count(*) from information_schema.tables where table_schema = 'public'" </dev/null)
	docker compose ps
	if [ "$tables" != 0 ]; then
		echo "install: WARNING: database is not empty ($tables tables); restore will refuse without RESTORE_FORCE=yes" >&2
	fi
	echo "install: prepared, only the empty database is running. Next: backup restore, then install.sh without --prepare"
	exit 0
fi
if $test_mode; then
	docker compose stop caddy 2>/dev/null || true
	docker compose up -d "${services[@]}"
else
	docker compose up -d --remove-orphans
fi
docker compose ps
echo "install: done. Next: deploy/portable/smoke.sh (see docs/disaster-recovery.md)"
