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
yc config set folder-id "$FOLDER_ID"

echo "== 1. YDB Serverless (лимит 200 RU/с — предохранитель счёта; при 10 RU/с по умолчанию выгрузка 1000 записей ≈ 6000 RU троттлится ~10 мин)"
# Проверить флаги на текущей версии CLI: yc ydb database create --help
yc ydb database create "$P-db" --serverless --sls-throughput-limit 200 --sls-storage-size 1GB
DB_ENDPOINT=$(yc ydb database get "$P-db" --format json | jq -r .endpoint)   # grpcs://...?database=/ru-central1/...
YDB_ENDPOINT="${DB_ENDPOINT%%/?database=*}"
YDB_DATABASE="${DB_ENDPOINT##*database=}"

echo "== 2. Сервисные аккаунты: runtime (контейнер → YDB, Lockbox) и gateway (шлюз → контейнер)"
yc iam service-account create --name "$P-runtime"
yc iam service-account create --name "$P-gateway"
RUNTIME_SA=$(yc iam service-account get "$P-runtime" --format json | jq -r .id)
GATEWAY_SA=$(yc iam service-account get "$P-gateway" --format json | jq -r .id)
# Минимальные роли на КОНКРЕТНЫЕ ресурсы, а не на каталог:
yc ydb database add-access-binding "$P-db" --role ydb.editor --service-account-id "$RUNTIME_SA"

echo "== 3. Секрет JWT в Lockbox (значение генерируется здесь и в репозиторий не попадает)"
JWT=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
yc lockbox secret create --name "$P-jwt" --payload "[{\"key\":\"jwt\",\"text_value\":\"$JWT\"}]" >/dev/null
unset JWT
SECRET_ID=$(yc lockbox secret get "$P-jwt" --format json | jq -r .id)
yc lockbox secret add-access-binding "$SECRET_ID" --role lockbox.payloadViewer --service-account-id "$RUNTIME_SA"

echo "== 4. Container Registry и образ"
yc container registry create --name "$P-registry"
REGISTRY_ID=$(yc container registry get "$P-registry" --format json | jq -r .id)
yc container registry add-access-binding "$REGISTRY_ID" --role container-registry.images.puller \
  --service-account-id "$RUNTIME_SA"
yc container registry configure-docker
IMAGE="cr.yandex/$REGISTRY_ID/sync-proto:$TAG"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

echo "== 5. Схема YDB (выполняет оператор своим IAM-токеном, не контейнер)"
YDB_AUTH=env YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)" ENV=cloud \
  YDB_ENDPOINT="$YDB_ENDPOINT" YDB_DATABASE="$YDB_DATABASE" \
  python -m syncproto.schema create --prefix ydbsync

echo "== 6. Serverless Container (приватный, scale-to-zero, без подготовленных экземпляров)"
yc serverless container create --name "$P-api"
yc serverless container revision deploy --container-name "$P-api" --image "$IMAGE" \
  --cores 1 --core-fraction 100 --memory 512MB --concurrency 4 --execution-timeout 30s \
  --service-account-id "$RUNTIME_SA" \
  --environment "ENV=cloud,YDB_ENDPOINT=$YDB_ENDPOINT,YDB_DATABASE=$YDB_DATABASE,YDB_AUTH=metadata,YDB_COLLECT_STATS=1,YDB_POOL_SIZE=4" \
  --secret "environment-variable=SYNC_JWT_SECRET,id=$SECRET_ID,key=jwt"
CONTAINER_ID=$(yc serverless container get "$P-api" --format json | jq -r .id)
# Вызов контейнера — только сервисному аккаунту шлюза. allUsers НЕ добавляем.
yc serverless container add-access-binding "$P-api" --role serverless.containers.invoker \
  --service-account-id "$GATEWAY_SA"

echo "== 7. API Gateway"
CONTAINER_ID="$CONTAINER_ID" GATEWAY_SA_ID="$GATEWAY_SA" envsubst < cloud/api-gateway.yaml > /tmp/$P-gw.yaml
yc serverless api-gateway create --name "$P-gw" --spec /tmp/$P-gw.yaml
GW_DOMAIN=$(yc serverless api-gateway get "$P-gw" --format json | jq -r .domain)

cat <<EOF

Готово. Дальше — измерения (cloud/README.md, шаг «Измерения»):
  export GATEWAY_URL=https://$GW_DOMAIN
  export SYNC_JWT_SECRET=\$(yc lockbox payload get --id $SECRET_ID --key jwt)
  python -m cloud.measure
  python -m bench.bench --target url --url \$GATEWAY_URL -n 30 --out cloud_benchmark.json
EOF
