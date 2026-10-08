#!/usr/bin/env bash
# Удаляет все ресурсы облачного эксперимента (имена из deploy.sh). Данные YDB удаляются безвозвратно —
# в базе только синтетические данные прототипа.
set -euo pipefail
: "${FOLDER_ID:?set FOLDER_ID}"
P="${NAME_PREFIX:-mp-sync-proto}"
yc config set folder-id "$FOLDER_ID"
yc serverless api-gateway delete "$P-gw" || true
yc serverless container delete "$P-api" || true
REGISTRY_ID=$(yc container registry get "$P-registry" --format json 2>/dev/null | jq -r .id || true)
if [[ -n "${REGISTRY_ID:-}" ]]; then
  for img in $(yc container image list --registry-id "$REGISTRY_ID" --format json | jq -r '.[].id'); do
    yc container image delete "$img"
  done
  yc container registry delete "$P-registry"
fi
yc lockbox secret delete "$P-jwt" || true
yc ydb database delete "$P-db" || true
yc iam service-account delete "$P-gateway" || true
yc iam service-account delete "$P-runtime" || true
echo "teardown done"
