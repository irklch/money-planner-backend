#!/usr/bin/env bash
# Установка на чистый сервер (Ubuntu 22.04/24.04, Debian 12; x86_64 или arm64; от 1 vCPU / 2 ГБ).
# Запуск из клона репозитория: sudo deploy/portable/install.sh
# Идемпотентен: повторный запуск обновляет образы и перезапускает сервисы.
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "install: $*" >&2; exit 1; }

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
[ -n "${BACKUP_REMOTE:-}" ] || echo "install: WARNING: BACKUP_REMOTE is empty, backups will stay on this server only" >&2

# Брандмауэр: снаружи только SSH и HTTP(S). Порт БД не публикуется compose вообще.
if command -v ufw >/dev/null; then
	ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
	ufw --force enable >/dev/null
fi

IMAGE_TAG=$(git -C ../.. rev-parse --short HEAD 2>/dev/null || echo local)
export IMAGE_TAG
docker compose build
docker compose up -d --remove-orphans
docker compose ps
echo "install: done. Next: deploy/portable/smoke.sh (see docs/disaster-recovery.md)"
