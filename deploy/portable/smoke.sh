#!/usr/bin/env bash
# Сквозная проверка сервера: healthz → гостевая сессия (запись в БД) → чтение категорий (чтение из БД).
# Создаёт одну тестовую guest-запись без финансовых данных; её удалит maintenance через 12 мес.
#
#   smoke.sh                         локально на сервере (http://127.0.0.1:8000)
#   smoke.sh https://api.example.ru  через домен
#   smoke.sh https://api.example.ru 203.0.113.10
#                                    через домен, но к указанному IP (проверка до переключения DNS)
set -euo pipefail

base=${1:-http://127.0.0.1:8000}
ip=${2:-}
curl_opts=(-sS --fail-with-body -m 20)
if [ -n "$ip" ]; then
	host=${base#*://}; host=${host%%/*}
	curl_opts+=(--resolve "$host:443:$ip" --resolve "$host:80:$ip")
fi
c() { curl "${curl_opts[@]}" "$@"; }

step() { printf '%-28s' "$1"; }

step "healthz"
c "$base/healthz" >/dev/null && echo ok

step "TLS certificate"
if [[ $base == https://* ]]; then
	c -o /dev/null -w '%{ssl_verify_result}\n' "$base/healthz" | grep -qx 0 && echo ok
else
	echo skipped
fi

step "guest session (DB write)"
key=$(python3 -c "import uuid; print(uuid.uuid4())")
session=$(c -X POST "$base/v1/auth/anonymous" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $key" -d '{"platform":"ios","appVersion":"smoke"}')
token=$(printf '%s' "$session" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')
echo ok

step "categories (DB read)"
n=$(c "$base/v1/categories" -H "Authorization: Bearer $token" -H 'X-Timezone: Europe/Moscow' |
	python3 -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))')
[ "$n" -gt 0 ] && echo "ok ($n system categories)"

step "idempotent replay"
c -o /dev/null -D - -X POST "$base/v1/auth/anonymous" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $key" -d '{"platform":"ios","appVersion":"smoke"}' |
	grep -qi '^idempotent-replayed: true' && echo ok

echo "smoke: all checks passed for $base${ip:+ via $ip}"
