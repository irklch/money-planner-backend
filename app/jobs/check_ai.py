"""Проверка подключения к Yandex AI Studio и идентификаторов моделей из конфигурации.

python -m app.jobs.check_ai
"""

import asyncio
import sys

from app.ai.yandex import YandexLLMClient
from app.core.config import get_settings


async def main() -> int:
    get_settings.cache_clear()
    client = YandexLLMClient(get_settings())
    await client.verify()
    print("AI models OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
