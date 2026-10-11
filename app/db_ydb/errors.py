"""Ошибки слоя YDB и их классификация.

Повторяемые ошибки (OCC-конфликт, перегрузка, недоступность, таймаут) повторяются внутри операции;
если бюджет повторов исчерпан, наружу выходит `YdbUnavailable` → `503 service_unavailable` +
`Retry-After` (контракт E0). Остальные ошибки YDB (схема, запрос, права) — ошибки программы или
конфигурации: они не повторяются и дают `500 internal_error`. Текст ошибок YDB в ответ и логи не
попадает: в нём могут быть значения параметров.
"""

import ydb

# Временные состояния YDB и транспорта: повтор той же операции может пройти.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ydb.issues.Aborted,
    ydb.issues.Unavailable,
    ydb.issues.Overloaded,
    ydb.issues.BadSession,
    ydb.issues.SessionExpired,
    ydb.issues.SessionBusy,
    ydb.issues.SessionPoolEmpty,
    ydb.issues.Timeout,
    ydb.issues.DeadlineExceed,
    ydb.issues.ConnectionError,
    ydb.issues.Undetermined,
    ydb.issues.Cancelled,
    TimeoutError,
)


class YdbError(Exception):
    """Базовая ошибка слоя YDB."""


class YdbUnavailable(YdbError):
    """YDB недоступна, перегружена или не уложилась в бюджет повторов → 503 + Retry-After."""

    def __init__(self, reason: str, retry_after: int = 1):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class YdbNotConfigured(YdbError):
    """YDB_ENDPOINT не задан, а операции нужна YDB."""


class MigrationError(YdbError):
    """Миграции не могут быть применены: изменён применённый файл, пропуск версии и т. п."""


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)


def reason_of(exc: BaseException) -> str:
    """Короткая безопасная причина для логов: только тип ошибки."""
    return type(exc).__name__
