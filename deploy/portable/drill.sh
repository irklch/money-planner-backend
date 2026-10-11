#!/usr/bin/env bash
# Учения по восстановлению в изолированном окружении: отдельный compose-проект, синтетические
# данные, «внешнее хранилище» — локальный каталог. Production и реальные секреты не используются.
#
#   deploy/portable/drill.sh
#
# Сценарий: поднять сервер → записать данные через API → зашифрованная копия → уничтожить сервер
# вместе с томами → новый сервер → восстановить последнюю копию → проверить данные и старую сессию.
set -euo pipefail
cd "$(dirname "$0")"

project=mp-drill-$$
work=$(mktemp -d)
port=${DRILL_PORT:-18080}
base=http://127.0.0.1:$port
dc() { docker compose -p "$project" -f docker-compose.yml -f "$work/override.yml" --env-file "$work/.env" "$@"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
t0=$(date +%s)
tick() { echo "[$(( $(date +%s) - t0 ))s] $*"; }

mkdir -p "$work/remote"
docker build -q -t money-planner-backup:local backup >/dev/null
docker run --rm --entrypoint age-keygen money-planner-backup:local 2>/dev/null >"$work/age.key"
recipient=$(grep -o 'age1[0-9a-z]*' "$work/age.key")
uuid() { python3 -c "import uuid; print(uuid.uuid4())"; }
rnd() { openssl rand -base64 48 | tr -d '\n/+='; }
cat >"$work/.env" <<EOF
API_DOMAIN=drill.localhost
FENCE_EXPECTED_IP=127.0.0.1
FENCE_MODE=off
INSTANCE_NAME=mp-drill
POSTGRES_PASSWORD=$(rnd)
JWT_SECRET=$(rnd)
TOKEN_DERIVATION_SECRET=$(rnd)
AI_ENABLED=false
BACKUP_AGE_RECIPIENT=$recipient
BACKUP_REMOTE=/remote
EOF
cat >"$work/override.yml" <<EOF
services:
  migrate: { env_file: !override [$work/.env] }
  maintenance: { env_file: !override [$work/.env] }
  api:
    env_file: !override [$work/.env]
    ports: !override ["127.0.0.1:$port:8000"]
  backup:
    env_file: !override [$work/.env]
    volumes: ["backups:/backups", "$work/remote:/remote", "$work/age.key:/run/age.key:ro"]
EOF

tick "build and start the first server"
dc up -d --build --wait db migrate api >/dev/null
./smoke.sh "$base" >/dev/null

tick "write synthetic data through the API"
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
session=$(curl -sS --fail-with-body -X POST "$base/v1/auth/anonymous" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $(uuid)" -d '{"platform":"ios","appVersion":"drill"}')
access=$(json '["accessToken"]' <<<"$session")
refresh=$(json '["refreshToken"]' <<<"$session")
auth=(-H "Authorization: Bearer $access" -H 'X-Timezone: Europe/Moscow' -H 'Content-Type: application/json')
for i in 1 2 3; do
	curl -sS --fail-with-body -o /dev/null -X POST "$base/v1/expenses" "${auth[@]}" -H "Idempotency-Key: $(uuid)" \
		-d "{\"date\":\"2026-09-0$i\",\"amount\":\"10$i.50\",\"categoryId\":\"00000000-0000-4000-8000-000000000001\",\"comment\":\"drill $i\"}"
done
before=$(curl -sS --fail-with-body "$base/v1/expenses?from=2026-09-01&to=2026-09-30" "${auth[@]}" | json '["items"]' | sort)

tick "encrypted backup to the off-site directory"
dc run --rm -T backup once
ls "$work/remote/mp-drill"
head -c 21 "$work/remote/mp-drill/"*.dump.age | grep -qx 'age-encryption.org/v1' ||
	{ echo "drill: backup is not age-encrypted"; exit 1; }

tick "destroy the first server with all volumes"
dc down -v >/dev/null

tick "new server: empty database, restore latest backup"
t_restore=$(date +%s)
dc up -d --wait db >/dev/null
dc run --rm -T backup restore latest
dc up -d --wait migrate api >/dev/null
tick "restore finished in $(( $(date +%s) - t_restore ))s"

tick "verify data and the pre-disaster session"
refreshed=$(curl -sS --fail-with-body -X POST "$base/v1/auth/refresh" -H 'Content-Type: application/json' \
	-d "{\"refreshToken\":\"$refresh\"}")
access=$(json '["accessToken"]' <<<"$refreshed")
auth=(-H "Authorization: Bearer $access" -H 'X-Timezone: Europe/Moscow')
after=$(curl -sS --fail-with-body "$base/v1/expenses?from=2026-09-01&to=2026-09-30" "${auth[@]}" | json '["items"]' | sort)
[ "$before" = "$after" ] || { echo "drill: FAILED, expenses differ"; echo "$before"; echo "$after"; exit 1; }
./smoke.sh "$base" >/dev/null

tick "drill passed: 3 expenses and the guest session survived a full server loss"
