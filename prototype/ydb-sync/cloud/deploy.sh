#!/usr/bin/env bash
# Облачный эксперимент: СОЗДАЁТ ПЛАТНЫЕ РЕСУРСЫ Yandex Cloud.
# Запускать только после явного подтверждения владельца проекта:
#   CONFIRM_PAID_RESOURCES=yes FOLDER_ID=b1g... ./cloud/deploy.sh
# Удаление всего созданного: ./cloud/teardown.sh
#
# Ожидаемая стоимость эксперимента (1–2 дня) — в cloud/README.md. Почти всё укладывается в
# бесплатный объём serverless; платно — Lockbox и хранение образа в Container Registry.
set -euo pipefail

if [[ "${CONFIRM_PAID_RESOURCES:-}" != "yes" ]]; then
  echo "Refusing to create paid resources: set CONFIRM_PAID_RESOURCES=yes after approval" >&2
  exit 1
fi
: "${FOLDER_ID:?set FOLDER_ID}"
P="${NAME_PREFIX:-mp-sync-proto}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
cd "$(dirname "$0")/.."
# Все команды — с явным --folder-id: глобальный профиль yc оператора не меняется.
export YC_FOLDER_ID="$FOLDER_ID"
# Явный endpoint: без него `yc serverless ...` в CLI 1.40 падает с «endpoint should be set».
Y() { yc --endpoint api.cloud.yandex.net:443 --folder-id "$FOLDER_ID" "$@"; }

echo "== 0. Предпроверка: ни одного ресурса с префиксом $P в каталоге (существующие ресурсы не трогаем)"
./cloud/preflight.sh

echo "== 1. YDB Serverless: лимит 10 RU/с (значение по умолчанию; неиспользованные RU копятся 5 мин)."
# Лимит не повышаем заранее: эксперимент как раз проверяет, хватает ли его для MVP.
# Явно: троттлинг включён (предохранитель счёта), выделенной ёмкости нет (нет почасовой оплаты),
# хранилище — 1 ГБ (бесплатный объём).
Y ydb database create "$P-db" --serverless --sls-storage-size 1GB \
  --sls-enable-throttling-rcu=true --sls-throttling-rcu 10 --sls-provisioned-rcu 0
DB_ENDPOINT=$(Y ydb database get "$P-db" --format json | jq -r .endpoint)   # grpcs://...?database=/ru-central1/...
YDB_ENDPOINT="${DB_ENDPOINT%%/?database=*}"
YDB_DATABASE="${DB_ENDPOINT##*database=}"

echo "== 2. Сервисные аккаунты: runtime (контейнер → YDB, Lockbox) и gateway (шлюз → контейнер)"
Y iam service-account create --name "$P-runtime"
Y iam service-account create --name "$P-gateway"
RUNTIME_SA=$(Y iam service-account get "$P-runtime" --format json | jq -r .id)
GATEWAY_SA=$(Y iam service-account get "$P-gateway" --format json | jq -r .id)
# Минимальные роли на КОНКРЕТНЫЕ ресурсы, а не на каталог:
Y ydb database add-access-binding "$P-db" --role ydb.editor --service-account-id "$RUNTIME_SA"

echo "== 3. Секрет JWT в Lockbox (значение генерируется здесь и в репозиторий не попадает)"
JWT=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
# Значение передаётся через stdin, а не аргументом: не видно в списке процессов.
printf '[{"key":"jwt","text_value":"%s"}]' "$JWT" | Y lockbox secret create --name "$P-jwt" --payload - >/dev/null
unset JWT
SECRET_ID=$(Y lockbox secret get "$P-jwt" --format json | jq -r .id)
Y lockbox secret add-access-binding "$SECRET_ID" --role lockbox.payloadViewer --service-account-id "$RUNTIME_SA"

echo "== 3b. Лог-группа прототипа (хранение 1 день), запись — только runtime-аккаунту"
Y logging group create --name "$P-logs" --retention-period 24h
Y logging group add-access-binding "$P-logs" --role logging.writer --service-account-id "$RUNTIME_SA"

echo "== 4. Container Registry и образ"
Y container registry create --name "$P-registry"
REGISTRY_ID=$(Y container registry get "$P-registry" --format json | jq -r .id)
Y container registry add-access-binding "$REGISTRY_ID" --role container-registry.images.puller \
  --service-account-id "$RUNTIME_SA"
# Вход в реестр краткоживущим IAM-токеном (12 ч), без изменения credential helper Docker.
yc iam create-token | docker login --username iam --password-stdin cr.yandex
IMAGE="cr.yandex/$REGISTRY_ID/sync-proto:$TAG"
docker build --platform linux/amd64 --provenance=false --sbom=false -t "$IMAGE" .
docker push "$IMAGE"

echo "== 5. Схема YDB (выполняет оператор своим IAM-токеном, не контейнер)"
YDB_AUTH=env YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)" ENV=cloud \
  YDB_ENDPOINT="$YDB_ENDPOINT" YDB_DATABASE="$YDB_DATABASE" \
  .venv/bin/python -m syncproto.schema create --prefix ydbsync

echo "== 6. Serverless Container (приватный, scale-to-zero, без подготовленных экземпляров)"
Y serverless container create --name "$P-api"
Y serverless container revision deploy --container-name "$P-api" --image "$IMAGE" \
  --cores 1 --core-fraction 100 --memory 512MB --concurrency 4 --execution-timeout 30s \
  --service-account-id "$RUNTIME_SA" \
  --environment "ENV=cloud,YDB_ENDPOINT=$YDB_ENDPOINT,YDB_DATABASE=$YDB_DATABASE,YDB_AUTH=metadata,YDB_COLLECT_STATS=1,YDB_POOL_SIZE=4" \
  --secret "environment-variable=SYNC_JWT_SECRET,id=$SECRET_ID,key=jwt" \
  --log-group-name "$P-logs" --min-log-level warn
CONTAINER_ID=$(Y serverless container get "$P-api" --format json | jq -r .id)
# Вызов контейнера — только сервисному аккаунту шлюза. allUsers НЕ добавляем.
Y serverless container add-access-binding "$P-api" --role serverless.containers.invoker \
  --service-account-id "$GATEWAY_SA"

echo "== 7. API Gateway"
CONTAINER_ID="$CONTAINER_ID" GATEWAY_SA_ID="$GATEWAY_SA" envsubst < cloud/api-gateway.yaml > "${TMPDIR:-/tmp}/$P-gw.yaml"
# Логи шлюза — в группу прототипа: иначе Cloud Logging создаёт в каталоге группу `default`.
Y serverless api-gateway create --name "$P-gw" --spec "${TMPDIR:-/tmp}/$P-gw.yaml" \
  --log-group-name "$P-logs" --min-log-level warn
rm -f "${TMPDIR:-/tmp}/$P-gw.yaml"
GW_DOMAIN=$(Y serverless api-gateway get "$P-gw" --format json | jq -r .domain)

cat <<EOF

Готово. Дальше — измерения (cloud/README.md, шаг «Измерения»):
  export GATEWAY_URL=https://$GW_DOMAIN
  export SYNC_JWT_SECRET=\$(yc lockbox payload get --id $SECRET_ID --key jwt)
  python -m cloud.e2e --restore 200 --bulk 1000
  python -m cloud.measure --idle 1,5,15 --history 200
Удаление: FOLDER_ID=$FOLDER_ID ./cloud/teardown.sh
EOF
