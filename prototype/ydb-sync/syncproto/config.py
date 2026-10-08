"""Настройки из окружения. Секретов по умолчанию нет: без SYNC_JWT_SECRET приложение не стартует."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "ydb"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    env: str = "local"  # local | test | cloud
    ydb_endpoint: str = "grpc://localhost:2136"
    ydb_database: str = "/local"
    ydb_auth: str = "anonymous"  # anonymous | metadata | sa-key-file | env
    ydb_sa_key_file: str | None = None
    ydb_table_prefix: str = "ydbsync"
    sync_strategy: str = "version"
    jwt_secret: str = ""
    jwt_audience: str = "money-planner-sync-proto"
    auto_schema: bool = False
    collect_stats: bool = False  # статистика строк/байт YDB в заголовках ответа (benchmark)

    @classmethod
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

    def validate(self, require_secret: bool = True) -> None:
        if self.env not in ("local", "test", "cloud"):
            raise ConfigError(f"unknown ENV={self.env}")
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
        if self.env == "cloud" and not self.ydb_endpoint.startswith("grpcs://"):
            raise ConfigError("cloud YDB endpoint must use TLS (grpcs://)")
        if self.env == "cloud" and self.auto_schema:
            raise ConfigError("schema is created by a separate step in cloud, not on cold start")
