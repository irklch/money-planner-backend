"""Слой доступа к YDB (план E1): подключение, пул 1–2 сессий, повторы, учёт RU, миграции.

Репозитории этапов E2–E7 получают `YdbClient` через зависимость `ydb_client` и работают через
`YdbClient.execute` (один запрос) или `YdbClient.transaction` (serializable-транзакция с повтором).
"""

from app.db_ydb.client import Tx, YdbClient, open_client
from app.db_ydb.errors import MigrationError, YdbError, YdbNotConfigured, YdbUnavailable

__all__ = [
    "MigrationError",
    "Tx",
    "YdbClient",
    "YdbError",
    "YdbNotConfigured",
    "YdbUnavailable",
    "open_client",
]
