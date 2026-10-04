"""Календарные даты и «сегодня» пользователя.

Даты расходов — календарные (YYYY-MM-DD), в UTC не преобразуются. Для проверки «дата ≤ сегодня»
клиент передаёт IANA-идентификатор часового пояса в заголовке X-Timezone (например Europe/Moscow);
«сегодня» вычисляется на сервере в этом поясе.
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ApiError, ErrorDetail
from app.core.security import utcnow

TIMEZONE_HEADER = "X-Timezone"


def parse_timezone(value: str | None) -> ZoneInfo:
    if not value:
        raise ApiError(
            "invalid_request", details=[ErrorDetail(code="timezone_required", field=TIMEZONE_HEADER)]
        )
    value = value.strip()
    # Только IANA-имена («Europe/Moscow», «UTC»), без путей и смещений вида «+03:00».
    if len(value) > 64 or value.startswith(("/", ".")) or ".." in value:
        raise ApiError(
            "invalid_request", details=[ErrorDetail(code="timezone_invalid", field=TIMEZONE_HEADER)]
        )
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(
            "invalid_request", details=[ErrorDetail(code="timezone_invalid", field=TIMEZONE_HEADER)]
        ) from None


def today_in(tz: ZoneInfo) -> date:
    return utcnow().astimezone(tz).date()


def iter_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
