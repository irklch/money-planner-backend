#!/usr/bin/env bash
# Предпроверка облачного эксперимента. ТОЛЬКО ЧТЕНИЕ: ничего не создаёт и не меняет.
#   FOLDER_ID=b1g... ./cloud/preflight.sh            (или FOLDER_NAME=money-planner)
# Печатает ресурсы каталога, которые мог бы затронуть эксперимент, и завершается с ошибкой,
# если в каталоге уже есть ресурс с именем прототипа (префикс mp-sync-proto) — их мы не перезаписываем.
set -euo pipefail
P="${NAME_PREFIX:-mp-sync-proto}"
if [[ -z "${FOLDER_ID:-}" ]]; then
  FOLDER_ID=$(yc resource-manager folder get --name "${FOLDER_NAME:-money-planner}" --format json | jq -r .id)
fi
Y() { yc --endpoint api.cloud.yandex.net:443 --folder-id "$FOLDER_ID" "$@" --format json; }
echo "Каталог: $FOLDER_ID ($(yc resource-manager folder get --id "$FOLDER_ID" --format json | jq -r .name))"
clash=0
show() {  # $1 — заголовок, остальное — команда yc, возвращающая список с полем name
  local title=$1; shift
  local names
  # Ошибка CLI — это «не проверено», а не «ресурсов нет»: останавливаемся.
  local raw
  if ! raw=$(Y "$@" 2>&1); then echo "ОШИБКА проверки ($title): $raw" >&2; exit 2; fi
  names=$(echo "$raw" | jq -r '.[]?.name')
  printf '%-28s %s\n' "$title:" "$(echo "${names:-—}" | paste -sd ',' - | sed 's/,/, /g')"
  if echo "$names" | grep -q "^$P"; then clash=1; fi
}
show "YDB" ydb database list
show "Serverless Containers" serverless container list
show "Cloud Functions" serverless function list
show "API Gateway" serverless api-gateway list
show "Lockbox" lockbox secret list
show "Container Registry" container registry list
show "Сервисные аккаунты" iam service-account list
show "VM" compute instance list
show "Managed PostgreSQL" managed-postgresql cluster list
show "Лог-группы" logging group list
if [[ $clash -ne 0 ]]; then
  echo "СТОП: в каталоге уже есть ресурсы с префиксом $P. Ничего не создаю." >&2
  exit 1
fi
echo "Ресурсов с префиксом $P нет — эксперимент создаст только новые ресурсы с этим префиксом."
