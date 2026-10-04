"""Идемпотентность POST /expenses/import: отпечаток тела и сохранённый результат (миграция 0003)."""

import asyncio
import json
import uuid

from sqlalchemy import func, select, text

from app.db.models import Expense, ExpenseImport
from app.db.session import sessionmaker
from app.modules.expenses.schemas import ImportRequest
from app.modules.expenses.service import import_request_hash
from tests.conftest import CAFE, PRODUCTS

URL = "/v1/expenses/import"


def body(n=3, amount="100.50", start_day=10):
    return {
        "expenses": [
            {
                "date": f"2026-08-{start_day + i:02d}",
                "amount": amount,
                "categoryId": PRODUCTS,
                "comment": f"ОП{i}",
            }
            for i in range(n)
        ]
    }


async def expense_count() -> int:
    async with sessionmaker()() as s:
        return await s.scalar(select(func.count()).select_from(Expense))


def h(headers, key):
    return {**headers, "Idempotency-Key": key}


# ---------- Нормализация отпечатка ----------


def test_hash_ignores_key_order_formatting_and_equivalent_values():
    raw = {"date": "2026-08-10", "amount": "100.5", "categoryId": PRODUCTS.upper(), "comment": "  ВКУСВИЛЛ "}
    a = ImportRequest.model_validate_json(json.dumps({"expenses": [raw]}, separators=(",", ":")))
    b = ImportRequest.model_validate_json(
        json.dumps(
            {
                "expenses": [
                    {"comment": "ВКУСВИЛЛ", "categoryId": PRODUCTS, "amount": "100.50", "date": "2026-08-10"}
                ]
            },
            indent=4,
            ensure_ascii=False,
        )
    )
    assert import_request_hash(a) == import_request_hash(b)

    # Отсутствующий, null и пустой комментарий эквивалентны.
    base = {"date": "2026-08-10", "amount": "1.00", "categoryId": PRODUCTS}
    variants = [base, {**base, "comment": None}, {**base, "comment": "   "}]
    hashes = {import_request_hash(ImportRequest.model_validate({"expenses": [v]})) for v in variants}
    assert len(hashes) == 1


def test_hash_detects_meaningful_differences():
    base = {"date": "2026-08-10", "amount": "1.00", "categoryId": PRODUCTS, "comment": "a"}
    ref = import_request_hash(ImportRequest.model_validate({"expenses": [base, {**base, "amount": "2.00"}]}))
    for changed in (
        [{**base, "amount": "1.01"}, {**base, "amount": "2.00"}],
        [{**base, "date": "2026-08-11"}, {**base, "amount": "2.00"}],
        [{**base, "categoryId": CAFE}, {**base, "amount": "2.00"}],
        [{**base, "comment": "b"}, {**base, "amount": "2.00"}],
        [{**base, "amount": "2.00"}, base],  # порядок операций — часть тела
        [base],
    ):
        assert import_request_hash(ImportRequest.model_validate({"expenses": changed})) != ref


# ---------- Повтор через API ----------


async def test_same_key_same_body_returns_stored_result(client, headers):
    key = str(uuid.uuid4())
    r1 = await client.post(URL, json=body(), headers=h(headers, key))
    # То же тело, но другой порядок ключей и эквивалентная запись суммы.
    same = {"expenses": [{**e, "amount": "100.5"} for e in body()["expenses"]]}
    r2 = await client.post(
        URL,
        content=json.dumps(same, indent=2),
        headers={**h(headers, key), "content-type": "application/json"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in r1.headers
    assert r2.json() == r1.json() == {"importedCount": 3, "dateFrom": "2026-08-10", "dateTo": "2026-08-12"}
    assert await expense_count() == 3

    async with sessionmaker()() as s:
        row = (await s.execute(text("SELECT * FROM expense_imports"))).mappings().one()
    assert row["imported_count"] == 3 and len(row["request_hash"]) == 32
    assert "ОП0" not in str(dict(row))  # строки выписки не хранятся


async def test_same_key_different_body_rejected(client, headers):
    key = str(uuid.uuid4())
    await client.post(URL, json=body(3), headers=h(headers, key))
    for other in (body(1), body(3, amount="100.51"), body(3, start_day=11)):
        r = await client.post(URL, json=other, headers=h(headers, key))
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
        assert r.json()["error"]["retryable"] is False
    assert await expense_count() == 3


async def test_replay_unaffected_by_later_changes(client, headers):
    key = str(uuid.uuid4())
    first = (await client.post(URL, json=body(3), headers=h(headers, key))).json()
    items = (await client.get("/v1/expenses?from=2026-08-01&to=2026-08-31", headers=headers)).json()["items"]
    await client.patch(
        f"/v1/expenses/{items[0]['id']}", json={"date": "2026-07-01", "amount": "1"}, headers=headers
    )
    await client.delete(f"/v1/expenses/{items[1]['id']}", headers=headers)
    # Категорию тоже можно архивировать — повтор проверяется до валидации.
    r = await client.post(URL, json=body(3), headers=h(headers, key))
    assert r.status_code == 201 and r.json() == first
    assert await expense_count() == 2  # ничего не воссоздано


async def test_parallel_same_key_same_body(client, headers):
    key = str(uuid.uuid4())
    rs = await asyncio.gather(
        *[client.post(URL, json=body(30, start_day=1), headers=h(headers, key)) for _ in range(6)]
    )
    created = [r for r in rs if r.status_code == 201 and "Idempotent-Replayed" not in r.headers]
    assert len(created) == 1
    for r in rs:
        if r.status_code == 201:
            assert r.json() == created[0].json()
        else:
            assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_in_progress"
            assert r.headers["Retry-After"]
    assert await expense_count() == 30
    # После завершения любой повтор — сохранённый результат.
    r = await client.post(URL, json=body(30, start_day=1), headers=h(headers, key))
    assert r.status_code == 201 and r.json() == created[0].json()


async def test_parallel_same_key_different_bodies(client, headers):
    key = str(uuid.uuid4())
    bodies = [body(n, start_day=1) for n in (5, 6, 7, 8)]
    rs = await asyncio.gather(*[client.post(URL, json=b, headers=h(headers, key)) for b in bodies])
    winners = [(b, r) for b, r in zip(bodies, rs, strict=True) if r.status_code == 201]
    assert len(winners) == 1
    win_body, win = winners[0]
    for r in rs:
        if r is not win:
            assert r.status_code == 409
            assert r.json()["error"]["code"] in ("idempotency_in_progress", "idempotency_key_reused")
    assert await expense_count() == len(win_body["expenses"]) == win.json()["importedCount"]
    for b in bodies:
        if b is not win_body:
            r = await client.post(URL, json=b, headers=h(headers, key))
            assert r.json()["error"]["code"] == "idempotency_key_reused"


async def test_hash_result_and_expenses_are_atomic(client, headers, monkeypatch):
    """Сбой посреди транзакции не оставляет ни записи идемпотентности, ни расходов."""
    from app.modules.expenses import service

    async def boom(*a, **kw):
        raise RuntimeError("db failure")

    monkeypatch.setattr(service, "clear_free_days", boom)
    key = str(uuid.uuid4())
    r = await client.post(URL, json=body(3), headers=h(headers, key))
    assert r.status_code == 500
    async with sessionmaker()() as s:
        assert await s.get(ExpenseImport, uuid.UUID(key)) is None
    assert await expense_count() == 0

    monkeypatch.undo()
    r = await client.post(URL, json=body(3), headers=h(headers, key))  # повтор выполняется как новый
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    assert await expense_count() == 3
