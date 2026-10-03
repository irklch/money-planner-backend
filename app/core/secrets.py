"""Загрузка секретов из Yandex Lockbox в переменные окружения при старте.

На VM сервисный аккаунт получает IAM-токен из metadata-сервиса; секрет LOCKBOX_SECRET_ID
содержит ключи вида DATABASE_URL, JWT_SECRET, TOKEN_DERIVATION_SECRET, AI_API_KEY.
Значения не логируются и не пишутся на диск.
"""

import logging
import os

import httpx

log = logging.getLogger("app.secrets")

METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
LOCKBOX_PAYLOAD_URL = "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets/{secret_id}/payload"


def fetch_metadata_iam_token(timeout: float = 3.0) -> str:
    resp = httpx.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["access_token"]


def load_lockbox_into_env() -> None:
    secret_id = os.environ.get("LOCKBOX_SECRET_ID")
    if not secret_id:
        return
    token = fetch_metadata_iam_token()
    resp = httpx.get(
        LOCKBOX_PAYLOAD_URL.format(secret_id=secret_id),
        headers={"Authorization": f"Bearer {token}"},
        timeout=5.0,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries", [])
    for entry in entries:
        key = entry["key"].upper()
        value = entry.get("textValue")
        if value is not None and key not in os.environ:
            os.environ[key] = value
    log.info("lockbox_loaded", extra={"count": len(entries)})
