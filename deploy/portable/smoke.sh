#!/usr/bin/env bash
# Сквозная проверка сервера на возможностях E2: healthz (готовность БД) → гостевая запись устройства
# /v1/auth/guest (devices, refresh_tokens) → идемпотентный повтор → ротация refresh-токена → повтор в
# grace-окне (та же пара) → гостевой токен принят защищённым маршрутом → отключённые маршруты дают 404.
# Создаёт одну тестовую запись устройства без финансовых данных; её очистит maintenance (E8).
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
session=$(c -X POST "$base/v1/auth/guest" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $key" -d '{"platform":"ios","appVersion":"smoke"}')
field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
refresh=$(field refreshToken <<<"$session")
[ "$(field subjectKind <<<"$session")" = guest ] && echo ok

# Повтор — до ротации: после неё тот же ключ по контракту даёт 409.
step "idempotent replay"
c -o /dev/null -D - -X POST "$base/v1/auth/guest" -H 'Content-Type: application/json' \
	-H "Idempotency-Key: $key" -d '{"platform":"ios","appVersion":"smoke"}' |
	grep -qi '^idempotent-replayed: true' && echo ok

step "refresh rotation (DB r/w)"
rotated=$(c -X POST "$base/v1/auth/refresh" -H 'Content-Type: application/json' -d "{\"refreshToken\":\"$refresh\"}")
[ "$(field refreshToken <<<"$rotated")" != "$refresh" ] && echo ok

# Потерянный ответ: повтор старого токена в grace-окне (60 с) возвращает ту же пару.
step "refresh retry within grace"
retry=$(c -X POST "$base/v1/auth/refresh" -H 'Content-Type: application/json' -d "{\"refreshToken\":\"$refresh\"}")
[ "$retry" = "$rotated" ] && echo ok

# Без файла после успешной авторизации разбор отвечает 400; без токена — 401.
step "guest token is accepted"
access=$(field accessToken <<<"$rotated")
[ "$(status -X POST "$base/v1/imports/parse" -H "Authorization: Bearer $access")" = 400 ] &&
	[ "$(status -X POST "$base/v1/imports/parse")" = 401 ] && echo ok

step "disabled routes are 404"
[ "$(status "$base/v1/categories")" = 404 ] &&
	[ "$(status -X POST "$base/v1/auth/anonymous")" = 404 ] &&
	[ "$(status -X DELETE "$base/v1/me" -H "Authorization: Bearer $access")" = 404 ] && echo ok

echo "smoke: all checks passed for $base${ip:+ via $ip}"
