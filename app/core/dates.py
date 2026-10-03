from datetime import date, timedelta

from app.core.config import get_settings
from app.core.security import utcnow


def latest_allowed_date() -> date:
    """«Сегодня» для проверки date ≤ сегодня.

    Клиент не передаёт часовой пояс, поэтому берём самую позднюю возможную календарную дату
    (UTC + max_client_utc_offset_hours). Дата «завтра» для любого пояса будет отклонена.
    """
    offset = timedelta(hours=get_settings().max_client_utc_offset_hours)
    return (utcnow() + offset).date()


def iter_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
