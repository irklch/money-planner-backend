# Развёртывание в Yandex Cloud

Всё размещается в `ru-central1`. Секреты хранятся только в Lockbox, в репозитории и в metadata VM их нет.

## 1. Сервисный аккаунт VM

```bash
yc iam service-account create --name money-planner-vm
SA_ID=$(yc iam service-account get money-planner-vm --format json | jq -r .id)
FOLDER_ID=$(yc config get folder-id)
for role in lockbox.payloadViewer container-registry.images.puller ai.languageModels.user \
            logging.writer monitoring.editor; do
  yc resourcemanager folder add-access-binding $FOLDER_ID --role $role --subject serviceAccount:$SA_ID
done
```

Для CI создайте отдельный аккаунт с ролью `container-registry.images.pusher`. Его JSON-ключ положите в GitHub secret `YC_SA_JSON_KEY`, а id реестра — в GitHub variable `YC_REGISTRY_ID`.

## 2. Container Registry

```bash
yc container registry create --name money-planner
```

## 3. Managed Service for PostgreSQL 16

- Версия 16, бэкапы хранятся 7 дней (`--backup-retain-period-days 7`).
- Пользователь `money`, база `money`. Пароль генерируется и сразу кладётся в Lockbox.
- Доступ только из подсети VM, подключение только по SSL. CA-сертификат Yandex лежит на VM в `/etc/money-planner/certs/root.crt`.

```bash
yc managed-postgresql cluster create --name money-planner-pg --environment production \
  --network-name default --host zone-id=ru-central1-a,subnet-name=default-ru-central1-a \
  --postgresql-version 16 --resource-preset s2.micro --disk-type network-ssd --disk-size 20 \
  --user name=money,password=<GENERATED> --database name=money,owner=money \
  --backup-retain-period-days 7
```

## 4. Lockbox

Создайте один секрет `money-planner-api` с такими ключами:

| Ключ | Значение |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://money:<pwd>@<host>:6432/money` |
| `JWT_SECRET` | ≥ 32 случайных байта (`openssl rand -base64 48`) |
| `TOKEN_DERIVATION_SECRET` | ≥ 32 случайных байта, **отличный** от JWT_SECRET |

При старте приложение читает секрет через IAM-токен сервисного аккаунта VM (`app/core/secrets.py`). Доступ к ИИ тоже идёт через IAM-токен VM, поэтому API-ключ не нужен.

## 5. Cloud Logging и Monitoring

```bash
yc logging group create --name money-planner --retention-period 336h   # 14 дней
```

Логи контейнеров собирает `fluentbit` из `docker-compose.prod.yml`. Приложение пишет JSON в stdout и не логирует суммы, описания операций, query string и токены.

Алерты в Monitoring заведите на: CPU и RAM VM, свободное место и число соединений PostgreSQL, отсутствие ответа `/healthz` (через Uptime check).

## 6. VM (Container Optimized Image)

```bash
yc compute instance create --name money-planner-api --zone ru-central1-a \
  --cores 2 --memory 4 --create-boot-disk image-folder-id=standard-images,image-family=container-optimized-image \
  --service-account-name money-planner-vm --network-interface subnet-name=default-ru-central1-a,nat-ip-version=ipv4 \
  --metadata-from-file docker-compose=deploy/docker-compose.prod.yml \
  --metadata YC_REGISTRY_ID=...,IMAGE_TAG=<sha>,LOCKBOX_SECRET_ID=...,YC_FOLDER_ID=...,YC_LOG_GROUP_ID=...,API_DOMAIN=api.example.ru
```

Положите на VM `/etc/money-planner/Caddyfile` (из `deploy/Caddyfile`) и `/etc/money-planner/certs/root.crt`. HTTPS-сертификат Caddy выпускает сам, для этого нужна A-запись домена на публичный IP VM.

Порядок старта: `migrate` выполняет `alembic upgrade head` и завершается, затем запускаются `api` и `caddy`. Сервис `maintenance` раз в сутки удаляет guest-пользователей, неактивных 12 месяцев.

## Что проверить перед первым запуском

- `python -m app.jobs.check_ai` на VM проверяет доступ к YandexGPT Lite и Pro и корректность идентификаторов моделей.
- Тег образа `cr.yandex/yc/fluent-bit-plugin-yandex` сверьте с актуальной документацией Cloud Logging.
