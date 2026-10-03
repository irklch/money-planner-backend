"""Адаптер Yandex AI Studio (Foundation Models, текстовая генерация).

- Lite — категоризация, Pro — формулирование выводов; URI моделей только из конфигурации.
- Каждый запрос с заголовком x-data-logging-enabled: false (провайдер не сохраняет данные).
- Ограниченные таймауты; ошибки провайдера → LLMError.
"""

import asyncio
import logging
import time

import httpx

from app.ai.client import LLMError, LLMMessage, LLMTask
from app.core.config import Settings
from app.core.secrets import fetch_metadata_iam_token

log = logging.getLogger("app.ai")

COMPLETION_PATH = "/foundationModels/v1/completion"


class YandexLLMClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        if not (settings.ai_folder_id and settings.ai_model_categorize and settings.ai_model_insights):
            raise LLMError("AI is not configured")
        self._s = settings
        self._models = {
            LLMTask.categorize: settings.ai_model_categorize,
            LLMTask.insights: settings.ai_model_insights,
        }
        self._http = http or httpx.AsyncClient(
            base_url=settings.ai_base_url,
            timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=5.0),
        )
        self._iam_token: str | None = None
        self._iam_expires = 0.0

    async def _auth_header(self) -> str:
        if self._s.ai_api_key is not None:
            return f"Api-Key {self._s.ai_api_key.get_secret_value()}"
        if self._s.ai_use_metadata_iam:
            if self._iam_token is None or time.monotonic() > self._iam_expires:
                self._iam_token = await asyncio.to_thread(fetch_metadata_iam_token)
                self._iam_expires = time.monotonic() + 3600
            return f"Bearer {self._iam_token}"
        raise LLMError("No AI credentials")

    async def complete(
        self, task: LLMTask, messages: list[LLMMessage], *, temperature: float = 0.1, max_tokens: int = 2000
    ) -> str:
        model_uri = self._models[task]
        body = {
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": str(max_tokens)},
            "messages": [{"role": m.role, "text": m.text} for m in messages],
            "jsonObject": True,
        }
        headers = {
            "Authorization": await self._auth_header(),
            "x-folder-id": self._s.ai_folder_id,
            "x-data-logging-enabled": "false",
        }
        started = time.perf_counter()
        try:
            resp = await self._http.post(COMPLETION_PATH, json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("llm_error", extra={"provider": "yandex", "reason": type(e).__name__})
            raise LLMError(type(e).__name__) from None
        elapsed = round((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            log.warning(
                "llm_error", extra={"provider": "yandex", "status": resp.status_code, "elapsed_ms": elapsed}
            )
            raise LLMError(f"status {resp.status_code}")
        log.info("llm_ok", extra={"provider": "yandex", "model": task.value, "elapsed_ms": elapsed})
        try:
            return resp.json()["result"]["alternatives"][0]["message"]["text"]
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError("unexpected response shape") from None

    async def verify(self) -> None:
        for task in LLMTask:
            await self.complete(task, [LLMMessage("user", 'Ответь JSON {"ok": true}')], max_tokens=20)
