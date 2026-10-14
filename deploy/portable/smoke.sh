#!/usr/bin/env bash
# Сквозная проверка сервера на возможностях E1: healthz (готовность БД) → гостевая сессия (запись в БД) →
# идемпотентный повтор → ротация refresh-токена (чтение и запись в БД) → отключённые маршруты дают 404.
# Гостевая сессия — старый /v1/auth/anonymous (таблицы legacy_*) до E2, где его заменит /v1/auth/guest.
# Создаёт одну тестовую guest-запись без финансовых данных; её удалит maintenance через 12 мес.
#
#   smoke.sh                         локально на сервере (http://127.0.0.1:8000)
#   smoke.sh https://api.example.ru  через домен
#   smoke.sh https://api.example.ru 203.0.113.10
#                                    через домен, но к указанному IP (проверка до переключения DNS)
set -euo pipefail

base=${1:-http://127.0.0.1:8000}
ip=${2:-}
resolve=()
if [ -n "$ip" ]; then
	host=${base#*://}; host=${host%%/*}
	resolve=(--resolve "$host:443:$ip" --resolve "$host:80:$ip")
fi
c() { curl -sS --fail-with-body -m 20 ${resolve[@]+"${resolve[@]}"} "$@"; }
status() { curl -sS -m 20 ${resolve[@]+"${resolve[@]}"} -o /dev/null -w '%{http_code}' "$@"; }

step() { printf '%-28s' "$1"; }

step "healthz (database ready)"
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
refresh=$(printf '%s' "$session" | python3 -c 'import json,sys; print(json.load(sys.stdin)["refreshToken"])')
echo ok

# Повтор — до ротации: после неё тот же ключ по контракту даёт 409.
step "idempotent replay"
c -o /dev/null -D - -X POST "$base/v1/auth/anonymous" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $key" -d '{"platform":"ios","appVersion":"smoke"}' |
	grep -qi '^idempotent-replayed: true' && echo ok

step "refresh rotation (DB r/w)"
rotated=$(c -X POST "$base/v1/auth/refresh" -H 'Content-Type: application/json' -d "{\"refreshToken\":\"$refresh\"}" |
	python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["refreshToken"] != "'"$refresh"'" and bool(d["accessToken"]))')
[ "$rotated" = True ] && echo ok

step "disabled routes are 404"
code=$(status "$base/v1/categories")
[ "$code" = 404 ] && echo ok

echo "smoke: all checks passed for $base${ip:+ via $ip}"
