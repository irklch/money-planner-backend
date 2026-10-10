#!/usr/bin/env bash
# Удаляет ТОЛЬКО ресурсы облачного эксперимента (имена с префиксом из deploy.sh) и проверяет,
# что их не осталось. Данные YDB удаляются безвозвратно — в базе только синтетические данные.
#   FOLDER_ID=b1g... ./cloud/teardown.sh
set -euo pipefail
: "${FOLDER_ID:?set FOLDER_ID}"
P="${NAME_PREFIX:-mp-sync-proto}"
# Явный endpoint: без него `yc serverless ...` в CLI 1.40 падает с «endpoint should be set».
Y() { yc --endpoint api.cloud.yandex.net:443 --folder-id "$FOLDER_ID" "$@"; }
# Удаление в обратном порядке зависимостей; «|| true» — ресурса уже может не быть (повторный запуск).
Y serverless api-gateway delete "$P-gw" || true
Y serverless container delete "$P-api" || true
# Реестр нельзя удалить, пока в нём есть образы, — сначала удаляем образы.
REGISTRY_ID=$(Y container registry get "$P-registry" --format json 2>/dev/null | jq -r .id || true)
if [[ -n "${REGISTRY_ID:-}" ]]; then
  for img in $(Y container image list --registry-id "$REGISTRY_ID" --format json | jq -r '.[].id'); do
    Y container image delete "$img"
  done
  Y container registry delete "$P-registry"
fi
Y lockbox secret delete "$P-jwt" || true
Y logging group delete "$P-logs" || true
Y ydb database delete "$P-db" || true
Y iam service-account delete "$P-gateway" || true
Y iam service-account delete "$P-runtime" || true
docker logout cr.yandex >/dev/null 2>&1 || true

echo "== Проверка: ресурсов с префиксом $P не осталось"
left=0
for cmd in "ydb database" "serverless container" "serverless api-gateway" "lockbox secret" \
           "container registry" "iam service-account" "logging group"; do
  # shellcheck disable=SC2086
  names=$(Y $cmd list --format json 2>/dev/null | jq -r '.[]?.name' | grep "^$P" || true)
  if [[ -n "$names" ]]; then echo "ОСТАЛОСЬ ($cmd): $names"; left=1; fi
done
# Lockbox удаляет секрет не мгновенно (статус DELETING) — повторите проверку позже, если он виден.
if [[ $left -ne 0 ]]; then echo "teardown НЕ завершён" >&2; exit 1; fi
echo "teardown done: ресурсов прототипа не осталось"
