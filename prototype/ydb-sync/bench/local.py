# Настройки для локального benchmark: локальная YDB, свой префикс таблиц, одноразовый JWT-секрет.

from __future__ import annotations

import os
import secrets

from syncproto.config import Settings

_SECRET = secrets.token_urlsafe(48)  # одноразовый секрет локального прогона


# Settings для заданного префикса таблиц.
def local_settings(prefix: str, collect: bool = False) -> Settings:
    return Settings(
        env="local",
        ydb_endpoint=os.environ.get("YDB_ENDPOINT", "grpc://localhost:2136"),
        ydb_database=os.environ.get("YDB_DATABASE", "/local"),
        ydb_table_prefix=prefix,
        jwt_secret=_SECRET,
    )
