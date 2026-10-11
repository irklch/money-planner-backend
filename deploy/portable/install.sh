#!/usr/bin/env bash
# Установка на чистый сервер (Ubuntu 22.04/24.04, Debian 12; x86_64 или arm64; от 1 vCPU / 2 ГБ).
# Запуск из клона репозитория: sudo deploy/portable/install.sh [--test]
# Идемпотентен: повторный запуск обновляет образы и перезапускает сервисы.
#
# --test  тестовый стенд без домена: Caddy не запускается, снаружи открыт только SSH,
#         API — на 127.0.0.1:8000 (доступ через ssh -L). Требует FENCE_MODE=off. Не для production.
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "install: $*" >&2; exit 1; }

test_mode=false
case "${1:-}" in
"") ;;
--test) test_mode=true ;;
*) die "usage: install.sh [--test]" ;;
esac

[ "$(id -u)" = 0 ] || die "run as root (sudo)"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
	# Пакеты дистрибутива: без внешних скриптов и без регистраций у Docker Inc.
	apt-get update -q
	apt-get install -y -q docker.io docker-compose-v2 || apt-get install -y -q docker.io docker-compose-plugin
	systemctl enable --now docker
fi

[ -f .env ] || die "create deploy/portable/.env from env.example first (chmod 600)"
chmod 600 .env
set -a; . ./.env; set +a
for v in API_DOMAIN FENCE_EXPECTED_IP INSTANCE_NAME POSTGRES_PASSWORD JWT_SECRET TOKEN_DERIVATION_SECRET BACKUP_AGE_RECIPIENT; do
	[ -n "${!v:-}" ] || die "$v is empty in .env"
done
[ "$JWT_SECRET" != "$TOKEN_DERIVATION_SECRET" ] || die "JWT_SECRET and TOKEN_DERIVATION_SECRET must differ"
if $test_mode; then
	[ "${FENCE_MODE:-}" = off ] || die "--test requires FENCE_MODE=off (no domain points to a test server)"
	services=(db migrate api maintenance backup)
else
	[ "${FENCE_MODE:-enforce}" = enforce ] || echo "install: WARNING: FENCE_MODE=$FENCE_MODE, protection against two primaries is off" >&2
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
if $test_mode; then
	docker compose stop caddy 2>/dev/null || true
	docker compose up -d "${services[@]}"
else
	docker compose up -d --remove-orphans
fi
docker compose ps
echo "install: done. Next: deploy/portable/smoke.sh (see docs/disaster-recovery.md)"
