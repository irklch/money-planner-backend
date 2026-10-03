"""Удаление guest после 12 месяцев неактивности (users.last_seen_at, схема БД).

Запускается по расписанию на VM (systemd timer / cron), без очередей:
    python -m app.jobs.purge_inactive_users
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.secrets import load_lockbox_into_env
from app.core.security import utcnow
from app.db.models import User
from app.db.session import dispose_engine, init_engine, sessionmaker

INACTIVITY = timedelta(days=365)
log = logging.getLogger("app.jobs")


async def purge() -> int:
    async with sessionmaker()() as session:
        result = await session.execute(delete(User).where(User.last_seen_at < utcnow() - INACTIVITY))
        await session.commit()
        return result.rowcount or 0


async def main() -> None:
    load_lockbox_into_env()
    get_settings.cache_clear()
    setup_logging(get_settings().log_level)
    init_engine()
    try:
        log.info("purge_inactive_users", extra={"count": await purge()})
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
