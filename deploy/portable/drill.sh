#!/usr/bin/env bash
# Учения по восстановлению в изолированном окружении: отдельный compose-проект, синтетические
# данные, «внешнее хранилище» — локальный каталог. Production и реальные секреты не используются.
#
#   deploy/portable/drill.sh
#
# Часть 1 повторяет docs/disaster-recovery.md §2–§3 командами Compose:
#   первый сервер → гостевая сессия через API → схема откатывается на предыдущую ревизию (копия «старой
#   версии», как на стенде до E1) → зашифрованная копия → сервер уничтожается вместе с томами →
#   новый сервер: только пустая БД (install.sh --prepare) → backup restore → миграции и приложение
#   (install.sh без --prepare) → проверка данных старой схемы, ревизии и старой сессии.
# Часть 2 (временно, до E2/E6): синтетические строки в таблицах MVP → копия → восстановление в отдельную
#   БД → совпадение числа строк и md5 содержимого. Через API эти таблицы пишутся только с E2/E6.
# Пароль БД намеренно содержит символы, которые ломают URL при простой подстановке.
set -euo pipefail
cd "$(dirname "$0")"

project=mp-drill-$$
work=$(mktemp -d)
port=${DRILL_PORT:-18080}
base=http://127.0.0.1:$port
dc() { docker compose -p "$project" -f docker-compose.yml -f "$work/override.yml" --env-file "$work/.env" "$@"; }
cleanup() {
	dc down -v --remove-orphans >/dev/null 2>&1 || true
	# Копии в $work/remote создаёт контейнер backup от root: на Linux удалить их может только root.
	if [ -d "$work/remote" ] && docker image inspect money-planner-backup:local >/dev/null 2>&1; then
		docker run --rm -v "$work/remote:/remote" --entrypoint sh money-planner-backup:local -c 'rm -rf /remote/*'
	fi
	rm -rf "$work"
}
trap cleanup EXIT
t0=$(date +%s)
tick() { echo "[$(( $(date +%s) - t0 ))s] $*"; }

mkdir -p "$work/remote"
docker build -q -t money-planner-backup:local backup >/dev/null
docker run --rm --entrypoint age-keygen money-planner-backup:local 2>/dev/null >"$work/age.key"
recipient=$(grep -o 'age1[0-9a-z]*' "$work/age.key")
uuid() { python3 -c "import uuid; print(uuid.uuid4())"; }
rnd() { openssl rand -base64 48 | tr -d '\n/+='; }
db_password="$(openssl rand -base64 30 | tr -d '\n')/+=@:%?&~"
cat >"$work/.env" <<EOF
API_DOMAIN=drill.localhost
FENCE_EXPECTED_IP=127.0.0.1
FENCE_MODE=off
INSTANCE_NAME=mp-drill
POSTGRES_PASSWORD=$db_password
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

json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
sql() { dc exec -T db psql -U money -d "${2:-money}" -v ON_ERROR_STOP=1 -At -c "$1" </dev/null; }
# Отпечаток содержимого таблиц: число строк и md5 строк в стабильном порядке.
fingerprint() {
	local db=$1 out="" t
	shift
	for t in "$@"; do
		out+="$t=$(sql "select count(*)||':'||coalesce(md5(string_agg(x::text, '|' order by x::text)), '-') from $t x" "$db") "
	done
	echo "$out"
}

# ---- Часть 1. Документированный сценарий (docs/disaster-recovery.md §2–§3) со сквозной сессией API ----
tick "part 1: guest session through the API (old /v1/auth/anonymous until E2)"
session=$(curl -sS --fail-with-body -X POST "$base/v1/auth/anonymous" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $(uuid)" -d '{"platform":"ios","appVersion":"drill"}')
refresh=$(json '["refreshToken"]' <<<"$session")
legacy_before=$(fingerprint money legacy_users legacy_refresh_tokens categories)

head_rev=$(sql "select version_num from alembic_version")
old_rev=$(dc run --rm -T --entrypoint python migrate -m alembic show head 2>/dev/null | sed -n 's/^Parent: //p')
[ -n "$old_rev" ] || { echo "drill: previous revision not found"; exit 1; }
tick "roll the schema back to $old_rev (backup of the schema before $head_rev, as on the old test stand)"
dc stop api >/dev/null
dc run --rm -T --entrypoint python migrate -m alembic downgrade "$old_rev" >/dev/null
[ "$(sql "select version_num from alembic_version")" = "$old_rev" ] || { echo "drill: downgrade failed"; exit 1; }

tick "encrypted backup to the off-site directory"
dc run --rm -T backup once
ls "$work/remote/mp-drill"
head -c 21 "$work/remote/mp-drill/"*.dump.age | grep -qx 'age-encryption.org/v1' ||
	{ echo "drill: backup is not age-encrypted"; exit 1; }

tick "destroy the first server with all volumes"
dc down -v >/dev/null

tick "new server, step 1 (install.sh --prepare): only the empty database"
t_restore=$(date +%s)
dc up -d --wait db >/dev/null
running=$(dc ps --status running --services | sort | tr '\n' ' ')
[ "$running" = "db " ] || { echo "drill: expected only db running, got: $running"; exit 1; }
[ "$(sql "select count(*) from information_schema.tables where table_schema = 'public'")" = 0 ] ||
	{ echo "drill: new database is not empty"; exit 1; }

tick "step 2: backup restore latest"
dc run --rm -T backup restore latest
[ "$(sql "select version_num from alembic_version")" = "$old_rev" ] || { echo "drill: restored revision mismatch"; exit 1; }

tick "step 3 (install.sh): migrations $old_rev -> $head_rev, then the application"
dc up -d --wait migrate api >/dev/null
[ "$(sql "select version_num from alembic_version")" = "$head_rev" ] || { echo "drill: migrations did not reach head"; exit 1; }
tick "restore finished in $(( $(date +%s) - t_restore ))s"

tick "verify: old-schema data survived the migration, the pre-disaster session still works"
[ "$(fingerprint money legacy_users legacy_refresh_tokens categories)" = "$legacy_before" ] ||
	{ echo "drill: FAILED, legacy data differs after restore and migration"; exit 1; }
curl -sS --fail-with-body -o /dev/null -X POST "$base/v1/auth/refresh" -H 'Content-Type: application/json' \
	-d "{\"refreshToken\":\"$refresh\"}"
./smoke.sh "$base" >/dev/null

# ---- Часть 2. Таблицы схемы MVP — на уровне БД ----
# Временный предел E1: таблицы MVP ещё не пишутся через API. Сквозная проверка возвращается этапами:
# устройства и refresh-токены через /v1/auth/guest — E2, данные синхронизации через /v1/sync/* — E6.
tick "part 2 (DB level until E2/E6): synthetic rows in the MVP tables"
u=$(uuid); d=$(uuid)
sql "
insert into users (user_id) values ('$u');
insert into devices (device_id, platform, app_version, account_id) values ('$d', 'ios', 'drill', '$u');
insert into refresh_tokens (token_hash, family_id, subject_kind, device_id, user_id, expires_at)
  values (repeat('a', 64), '$(uuid)', 'account', '$d', '$u', now() + interval '365 days');
insert into identities (provider, subject, user_id) values ('apple', 'drill.$u', '$u');
insert into sync_state (user_id, last_version) values ('$u', 3);
insert into sync_records (user_id, entity_type, entity_id, version, created_at, updated_at, order_ts, device_id,
  schema_version, mutation_id, payload)
  select '$u', t, gen_random_uuid(), v, now(), now(), now(), '$d', 1, gen_random_uuid(),
         jsonb_build_object('comment', 'drill ' || v, 'amountMinor', '10' || v || '50')
  from (values ('category', 1), ('expense', 2), ('day_mark', 3)) x(t, v);
insert into usage_counters (subject, feature, used) values ('device:$d', 'imports', 2);
insert into import_ledger (import_id, device_id, account_id, confirmed_at) values ('$(uuid)', '$d', '$u', now());
" >/dev/null
mvp_tables=(users devices refresh_tokens identities sync_state sync_records usage_counters import_ledger)
mvp_before=$(fingerprint money "${mvp_tables[@]}")
dc run --rm -T backup once >/dev/null
sql "drop database if exists restore_check" postgres >/dev/null
sql "create database restore_check" postgres >/dev/null
dc run --rm -T -e PGDATABASE=restore_check backup restore latest
[ "$(fingerprint restore_check "${mvp_tables[@]}")" = "$mvp_before" ] ||
	{ echo "drill: FAILED, MVP tables differ after restore"; exit 1; }
[ "$(sql "select version_num from alembic_version" restore_check)" = "$head_rev" ] ||
	{ echo "drill: restored revision mismatch (part 2)"; exit 1; }
sql "drop database restore_check" postgres >/dev/null

tick "drill passed: $old_rev backup -> empty db -> restore -> migrations to $head_rev, session survived; MVP tables restored identically (DB level)"
