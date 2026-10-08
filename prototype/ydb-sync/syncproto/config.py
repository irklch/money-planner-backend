"""Настройки из окружения. Секретов по умолчанию нет: без SYNC_JWT_SECRET приложение не стартует."""

from __future__ import annotations

# os — переменные окружения; urlparse — проверка хоста эндпоинта YDB.
import os
from dataclasses import dataclass
from urllib.parse import urlparse

# Хосты, которые считаются локальной YDB (Docker на этой машине или сервис ydb в compose).
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "ydb"}


# Неверная или небезопасная конфигурация — приложение не стартует.
class ConfigError(RuntimeError):
    pass


# Все настройки прототипа; имена переменных окружения — в from_env.
@dataclass(frozen=True)
class Settings:
    env: str = "local"  # local | test | cloud
    # Где YDB: эндпоинт gRPC и путь базы.
    ydb_endpoint: str = "grpc://localhost:2136"
    ydb_database: str = "/local"
    # Как аутентифицироваться в YDB: anonymous — локально; metadata — в Serverless Containers;
    # sa-key-file — ключ сервисного аккаунта из файла; env — токен из переменной (создание схемы).
    ydb_auth: str = "anonymous"  # anonymous | metadata | sa-key-file | env
    ydb_sa_key_file: str | None = None
    # Префикс (папка) таблиц — разные префиксы для приложения, тестов и benchmark.
    ydb_table_prefix: str = "ydbsync"
    # Стратегия конфликтов (см. resolve.py); выбранная — version.
    sync_strategy: str = "version"
    # Секрет подписи JWT и аудитория токенов.
    jwt_secret: str = ""
    jwt_audience: str = "money-planner-sync-proto"
    # Создавать таблицы при старте — только локально.
    auto_schema: bool = False
    collect_stats: bool = False  # статистика строк/байт YDB в заголовках ответа (benchmark)

    @classmethod
    # Прочитать настройки из окружения и проверить. Для CLI схемы секрет JWT не нужен.
    def from_env(cls, require_secret: bool = True) -> Settings:
        e = os.environ
        s = cls(
            env=e.get("ENV", "local"),
            ydb_endpoint=e.get("YDB_ENDPOINT", cls.ydb_endpoint),
            ydb_database=e.get("YDB_DATABASE", cls.ydb_database),
            ydb_auth=e.get("YDB_AUTH", cls.ydb_auth),
            ydb_sa_key_file=e.get("YDB_SA_KEY_FILE") or None,
            ydb_table_prefix=e.get("YDB_TABLE_PREFIX", cls.ydb_table_prefix),
            sync_strategy=e.get("SYNC_STRATEGY", cls.sync_strategy),
            jwt_secret=e.get("SYNC_JWT_SECRET", ""),
            auto_schema=e.get("YDB_AUTO_SCHEMA", "0") == "1",
            collect_stats=e.get("YDB_COLLECT_STATS", "0") == "1",
        )
        s.validate(require_secret)
        return s

    # Проверки безопасности конфигурации.
    def validate(self, require_secret: bool = True) -> None:
        if self.env not in ("local", "test", "cloud"):
            raise ConfigError(f"unknown ENV={self.env}")
        # Без длинного секрета токены можно подделать.
        if require_secret and len(self.jwt_secret) < 32:
            raise ConfigError("SYNC_JWT_SECRET must be set (>= 32 chars); it is never stored in the repo")
        host = urlparse(self.ydb_endpoint).hostname or ""
        if self.ydb_auth == "anonymous":
            # Анонимный доступ — только к локальной YDB в Docker.
            if self.env == "cloud" or host not in LOCAL_HOSTS:
                raise ConfigError("anonymous YDB auth is allowed only for a local YDB")
        elif self.ydb_auth == "sa-key-file":
            if not self.ydb_sa_key_file:
                raise ConfigError("YDB_SA_KEY_FILE is required for YDB_AUTH=sa-key-file")
        elif self.ydb_auth not in ("metadata", "env"):
            raise ConfigError(f"unknown YDB_AUTH={self.ydb_auth}")
        # В облаке — только TLS.
        if self.env == "cloud" and not self.ydb_endpoint.startswith("grpcs://"):
            raise ConfigError("cloud YDB endpoint must use TLS (grpcs://)")
        # Создание таблиц при каждом холодном старте замедлило бы его; в облаке схема создаётся отдельно.
        if self.env == "cloud" and self.auto_schema:
            raise ConfigError("schema is created by a separate step in cloud, not on cold start")
