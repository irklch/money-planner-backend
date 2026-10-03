"""Парсинг выписок. Банковский адаптер здесь — тестовый (не реальный банк), на синтетических данных."""

import io
import tempfile

import openpyxl
import pytest

from app.ai.client import LLMError, LLMTask
from app.ai.provider import set_llm_client
from app.modules.imports.masking import mask_pii
from app.modules.imports.parsing import registry
from app.modules.imports.parsing.tabular import TabularAdapter
from tests.conftest import CAFE, PRODUCTS


class FakeBank(TabularAdapter):
    code = "test_bank"
    name = "Тестовый банк"
    date_headers = ("дата операции",)
    amount_headers = ("сумма операции",)
    description_headers = ("описание",)
    status_headers = ("статус",)
    skip_statuses = ("failed",)


HEADER = ["Дата операции", "Сумма операции", "Описание", "Статус"]
ROWS = [
    ["01.09.2026 12:30:00", "-1 250,00", "ВКУСВИЛЛ", "OK"],
    ["02.09.2026", "-450.50", "Кофейня Зерно", "OK"],
    ["03.09.2026", "5 000,00", "Зарплата", "OK"],
    ["04.09.2026", "-10,00", "Отклонено", "FAILED"],
    ["05.09.2026", "abc", "Битая строка", "OK"],
    ["06.09.2026", "-300,00", "Перевод для Иван И.", "OK"],
]


@pytest.fixture(autouse=True)
def fake_bank():
    registry.register(FakeBank())
    yield
    registry.unregister("test_bank")


def xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Выписка по счёту"])
    ws.append([])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(rows, encoding="cp1251") -> bytes:
    return "\n".join(";".join(r) for r in rows).encode(encoding)


async def upload(client, headers, data: bytes, filename: str):
    return await client.post("/v1/imports/parse", files={"file": (filename, data)}, headers=headers)


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


async def test_parse_xlsx_rules_only(client, headers):
    r = await upload(client, headers, xlsx([HEADER, *ROWS]), "statement.xlsx")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bank"] == {"code": "test_bank", "name": "Тестовый банк"}
    assert body["period"] == {"from": "2026-09-01", "to": "2026-09-06"}
    ops = body["operations"]
    assert [o["amount"] for o in ops] == ["1250.00", "450.50", "300.00"]
    assert ops[0] == {
        "date": "2026-09-01",
        "amount": "1250.00",
        "comment": "ВКУСВИЛЛ",
        "categoryId": PRODUCTS,
        "categoryStatus": "assigned",
    }
    assert ops[1]["categoryStatus"] == "unassigned" and ops[1]["categoryId"] is None  # ИИ выключен
    assert body["skippedIncomeCount"] == 1
    assert body["warnings"] == [
        {"code": "partially_read", "unreadDates": ["2026-09-05"], "unreadRowCount": 1}
    ]


async def test_parse_csv_cp1251_with_llm(client, headers):
    llm = FakeLLM('{"items":[{"i":0,"c":1,"conf":"medium"},{"i":1,"c":null,"conf":"low"}]}')
    set_llm_client(llm)
    r = await upload(client, headers, csv_bytes([HEADER, *ROWS]), "выписка.csv")
    assert r.status_code == 200, r.text
    ops = r.json()["operations"]
    assert ops[1]["categoryId"] == CAFE and ops[1]["categoryStatus"] == "suggested"
    assert ops[2]["categoryStatus"] == "unassigned"
    # В ИИ ушли только маскированные названия — без сумм, дат, имён
    task, messages = llm.calls[0]
    sent = " ".join(m.text for m in messages)
    assert task == LLMTask.categorize
    assert "Иван" not in sent and "450" not in sent and "2026" not in sent and "ВКУСВИЛЛ" not in sent


async def test_llm_invalid_response_degrades_to_unassigned(client, headers):
    set_llm_client(FakeLLM("не json"))
    r = await upload(client, headers, xlsx([HEADER, *ROWS]), "s.xlsx")
    assert r.status_code == 200
    assert r.json()["operations"][1]["categoryStatus"] == "unassigned"


async def test_llm_down_is_processing_failed(client, headers):
    set_llm_client(FakeLLM(error=True))
    r = await upload(client, headers, xlsx([HEADER, *ROWS]), "s.xlsx")
    assert r.status_code == 500
    assert r.json()["error"] == {**r.json()["error"], "code": "processing_failed", "retryable": True}


@pytest.mark.parametrize(
    ("data", "name", "status", "code"),
    [
        (b"%PDF-1.4", "statement.pdf", 415, "unsupported_format"),
        (b"\xd0\xcf\x11\xe0legacy", "statement.xls", 415, "unsupported_format"),
        (b"not a zip", "statement.xlsx", 422, "corrupted_file"),
        (b"PK\x03\x04broken", "statement.xlsx", 422, "corrupted_file"),
        (csv_bytes([["Дата", "Сумма"], ["01.09.2026", "-1"]]), "s.csv", 422, "unknown_bank"),
        (csv_bytes([HEADER]), "s.csv", 422, "empty_statement"),
        (csv_bytes([HEADER, ["01.09.2026", "100,00", "Зарплата", "OK"]]), "s.csv", 422, "income_only"),
        (b"", "s.csv", 422, "empty_statement"),
    ],
)
async def test_parse_errors(client, headers, data, name, status, code):
    r = await upload(client, headers, data, name)
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code
    assert "ВКУСВИЛЛ" not in r.text and "Зарплата" not in r.text


async def test_unknown_bank_without_registered_adapters(client, headers):
    registry.unregister("test_bank")
    r = await upload(client, headers, xlsx([HEADER, *ROWS]), "s.xlsx")
    assert r.json()["error"]["code"] == "unknown_bank"
    registry.register(FakeBank())


async def test_file_too_large(client, headers, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1024)
    r = await upload(client, headers, b"x" * 5000, "s.csv")
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


async def test_no_temp_files_written(client, headers, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("temporary file created")

    for name in ("SpooledTemporaryFile", "NamedTemporaryFile", "TemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, forbidden)
    big = xlsx([HEADER, *ROWS * 3000])  # > 1 МБ не требуется: проверяем сам путь обработки
    r = await upload(client, headers, big, "s.xlsx")
    assert r.status_code == 200


async def test_parse_rate_limited(client, headers, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_parse_per_minute", 1)
    await upload(client, headers, xlsx([HEADER, *ROWS]), "s.xlsx")
    r = await upload(client, headers, xlsx([HEADER, *ROWS]), "s.xlsx")
    assert r.status_code == 429 and r.headers["Retry-After"]


def test_mask_pii():
    cases = {
        "Оплата 4276 3800 1234 5678 PYATEROCHKA": "4276",
        "Перевод для Иван И.": "Иван",
        "Перевод по номеру телефона +7 (916) 123-45-67": "916",
        "Счёт 40817810099910004312 пополнение": "40817810099910004312",
        "Иванов Пётр Сергеевич оплата": "Иванов",
        "Карта *1234 ЛЕНТА": "*1234",
        "Оплата mail@example.com": "mail@",
        "Петров И.И. перевод": "Петров",
    }
    for src, secret in cases.items():
        assert secret not in mask_pii(src), (src, mask_pii(src))
    assert "ВКУСВИЛЛ" in mask_pii("Оплата ВКУСВИЛЛ")
