import json
import logging

import httpx
import pytest

from app.ai.client import LLMError, LLMMessage, LLMTask
from app.ai.provider import set_llm_client
from app.ai.yandex import YandexLLMClient
from app.core.config import Settings
from tests.conftest import CAFE, PRODUCTS, add_expense


class FakeLLM:
    def __init__(self, answer=None, error=False):
        self.answer, self.error, self.calls = answer, error, []

    async def complete(self, task, messages, **kw):
        self.calls.append((task, messages))
        if self.error:
            raise LLMError("down")
        return self.answer

    async def verify(self):
        pass


URL = "/v1/insights?from=2026-09-01&to=2026-09-30&compareFrom=2026-08-01&compareTo=2026-08-31"


async def seed(client, headers):
    await add_expense(client, headers, "2026-08-05", "1000.00", CAFE, "секретный комментарий")
    await add_expense(client, headers, "2026-09-05", "1340.00", CAFE)
    await add_expense(client, headers, "2026-09-06", "660.00", PRODUCTS)


async def test_insights_numbers_validated(client, headers):
    await seed(client, headers)
    answer = {
        "items": [
            {
                "title": "На кафе ушло на 34% больше",
                "detail": "1 340 ₽ против прошлого периода",
                "target": "category",
                "category": 0,
            },
            {"title": "Выдуманное", "detail": "Вы сэкономили 999 ₽", "target": "period"},
            {"title": "Неверная ссылка", "detail": "Всего 2 000 ₽", "target": "category", "category": 7},
            {"title": "Всего 2 000 ₽", "detail": "Это на 100% больше", "target": "period"},
        ]
    }
    llm = FakeLLM(json.dumps(answer, ensure_ascii=False))
    set_llm_client(llm)
    r = await client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["title"] for i in items] == ["На кафе ушло на 34% больше", "Всего 2 000 ₽"]
    assert items[0]["target"] == {"type": "category", "categoryId": CAFE}
    assert items[1]["target"] == {"type": "period"}
    task, messages = llm.calls[0]
    sent = " ".join(m.text for m in messages)
    assert task == LLMTask.insights
    assert "секретный" not in sent  # comment в ИИ не передаётся
    assert "34%" in sent  # проценты считает backend


async def test_insights_unavailable(client, headers):
    await seed(client, headers)
    r = await client.get(URL, headers=headers)  # ИИ выключен конфигурацией
    assert r.status_code == 503 and r.json()["error"]["code"] == "insights_unavailable"
    set_llm_client(FakeLLM(error=True))
    r = await client.get(URL, headers=headers)
    assert r.status_code == 503 and r.json()["error"]["retryable"] is True


async def test_insights_invalid_json_gives_empty(client, headers):
    await seed(client, headers)
    set_llm_client(FakeLLM("бла"))
    r = await client.get(URL, headers=headers)
    assert r.status_code == 200 and r.json() == {"items": []}


async def test_insights_no_data_no_llm_call(client, headers):
    llm = FakeLLM("{}")
    set_llm_client(llm)
    r = await client.get(URL, headers=headers)
    assert r.json() == {"items": []} and llm.calls == []


async def test_yandex_adapter_headers_and_parsing():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"alternatives": [{"message": {"text": '{"ok": true}'}}]}})

    settings = Settings(
        ai_enabled=True,
        ai_folder_id="b1gfolder",
        ai_api_key="k",
        ai_model_categorize="gpt://b1gfolder/yandexgpt-lite/latest",
        ai_model_insights="gpt://b1gfolder/yandexgpt/latest",
    )
    client = YandexLLMClient(
        settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://llm.test")
    )
    text = await client.complete(LLMTask.categorize, [LLMMessage("user", "x")])
    assert text == '{"ok": true}'
    assert seen["headers"]["x-data-logging-enabled"] == "false"
    assert seen["headers"]["x-folder-id"] == "b1gfolder"
    assert seen["body"]["modelUri"] == "gpt://b1gfolder/yandexgpt-lite/latest"
    await client.complete(LLMTask.insights, [LLMMessage("user", "x")])
    assert seen["body"]["modelUri"] == "gpt://b1gfolder/yandexgpt/latest"


async def test_yandex_adapter_errors():
    def handler(request):
        return httpx.Response(500, text="boom")

    settings = Settings(
        ai_folder_id="f",
        ai_api_key="k",
        ai_model_categorize="gpt://f/yandexgpt-lite/latest",
        ai_model_insights="gpt://f/yandexgpt/latest",
    )
    client = YandexLLMClient(
        settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x")
    )
    with pytest.raises(LLMError):
        await client.complete(LLMTask.categorize, [LLMMessage("user", "x")])


def test_model_uri_validated():
    with pytest.raises(ValueError):
        Settings(ai_model_categorize="yandexgpt-lite")


async def test_logs_have_no_sensitive_data(client, headers, caplog):
    caplog.set_level(logging.DEBUG)
    await add_expense(client, headers, "2026-09-05", "4321.99", PRODUCTS, "ПЕРСОНАЛЬНЫЙ-КОММЕНТ")
    await client.get("/v1/expenses?from=2026-09-01&to=2026-09-30", headers=headers)
    await client.post("/v1/auth/refresh", json={"refreshToken": "z" * 40})
    from app.core.logging import JsonFormatter

    fmt = JsonFormatter()
    app_records = [
        r for r in caplog.records if r.name.startswith(("app", "uvicorn", "sqlalchemy", "alembic"))
    ]
    assert app_records  # access-лог приложения есть
    blob = "\n".join(fmt.format(r) for r in app_records)
    for secret in ("4321", "ПЕРСОНАЛЬНЫЙ", "zzzz", headers["Authorization"][7:27], "2026-09-01"):
        assert secret not in blob, secret
