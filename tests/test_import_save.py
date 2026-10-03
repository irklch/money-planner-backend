import asyncio
import uuid

from sqlalchemy import func, select

from app.db.models import Expense, ExpenseImport
from app.db.session import sessionmaker
from tests.conftest import CAFE, PRODUCTS, add_expense, auth, new_guest


def payload(n=3, category=PRODUCTS):
    return {
        "expenses": [
            {
                "date": f"2026-08-{i % 20 + 10:02d}",
                "amount": f"{100 + i}.50",
                "categoryId": category,
                "comment": f"ОП{i}",
            }
            for i in range(n)
        ]
    }


async def count_expenses() -> int:
    async with sessionmaker()() as s:
        return await s.scalar(select(func.count()).select_from(Expense))


async def test_import_saves_and_clears_free_days(client, headers):
    await client.put("/v1/calendar/acknowledgements/2026-08-11", headers=headers)
    await add_expense(client, headers, "2026-08-10", "1.00")  # пересечение по дню не мешает
    key = str(uuid.uuid4())
    r = await client.post("/v1/expenses/import", json=payload(), headers={**headers, "Idempotency-Key": key})
    assert r.status_code == 201, r.text
    assert r.json() == {"importedCount": 3, "dateFrom": "2026-08-10", "dateTo": "2026-08-12"}
    days = (await client.get("/v1/calendar/days?from=2026-08-10&to=2026-08-12", headers=headers)).json()[
        "days"
    ]
    assert [d["expenseCount"] for d in days] == [2, 1, 1]
    assert not any(d["isAcknowledgedEmpty"] for d in days)
    items = (await client.get("/v1/expenses?from=2026-08-01&to=2026-08-31", headers=headers)).json()["items"]
    assert {i["source"] for i in items} == {"manual", "import"}


async def test_retry_after_timeout_no_duplicates(client, headers):
    key = str(uuid.uuid4())
    h = {**headers, "Idempotency-Key": key}
    r1 = await client.post("/v1/expenses/import", json=payload(), headers=h)
    r2 = await client.post("/v1/expenses/import", json=payload(), headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert r1.json() == r2.json()
    assert await count_expenses() == 3


async def test_parallel_same_key_no_duplicates(client, headers):
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    rs = await asyncio.gather(
        *[client.post("/v1/expenses/import", json=payload(50), headers=h) for _ in range(4)]
    )
    codes = sorted(r.status_code for r in rs)
    assert codes.count(201) >= 1
    assert all(r.status_code == 201 or r.json()["error"]["code"] == "idempotency_in_progress" for r in rs)
    for r in rs:
        if r.status_code == 409:
            assert r.headers.get("Retry-After")
    assert await count_expenses() == 50


async def test_same_statement_new_key_creates_new_expenses(client, headers):
    for _ in range(2):
        r = await client.post(
            "/v1/expenses/import", json=payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        assert r.status_code == 201
    assert await count_expenses() == 6  # дедупликации нет (FD-03)


async def test_atomic_rejection_with_index(client, headers):
    cat = (
        await client.post(
            "/v1/categories",
            json={"name": "Архив", "emoji": "📁"},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
    ).json()
    await client.delete(f"/v1/categories/{cat['id']}", headers=headers)
    body = payload(3)
    body["expenses"][1]["categoryId"] = cat["id"]
    body["expenses"][2]["amount"] = "-5"
    key = str(uuid.uuid4())
    r = await client.post("/v1/expenses/import", json=body, headers={**headers, "Idempotency-Key": key})
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert {"index": 2, "field": "amount", "code": "amount_invalid"} in details
    assert await count_expenses() == 0
    body["expenses"][2]["amount"] = "5"
    r = await client.post("/v1/expenses/import", json=body, headers={**headers, "Idempotency-Key": key})
    assert r.status_code == 422
    assert {"index": 1, "field": "categoryId", "code": "category_archived"} in r.json()["error"]["details"]
    assert await count_expenses() == 0
    async with sessionmaker()() as s:
        assert await s.get(ExpenseImport, uuid.UUID(key)) is None  # запись идемпотентности не создана
    body["expenses"][1]["categoryId"] = CAFE
    r = await client.post("/v1/expenses/import", json=body, headers={**headers, "Idempotency-Key": key})
    assert r.status_code == 201 and await count_expenses() == 3


async def test_empty_and_too_large(client, headers, monkeypatch):
    r = await client.post(
        "/v1/expenses/import",
        json={"expenses": []},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_import_expenses", 2)
    r = await client.post(
        "/v1/expenses/import", json=payload(3), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


async def test_key_of_other_user_rejected(client, headers):
    key = str(uuid.uuid4())
    await client.post("/v1/expenses/import", json=payload(), headers={**headers, "Idempotency-Key": key})
    other = auth(await new_guest(client))
    r = await client.post("/v1/expenses/import", json=payload(), headers={**other, "Idempotency-Key": key})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert await count_expenses() == 3


async def test_import_requires_key(client, headers):
    r = await client.post("/v1/expenses/import", json=payload(), headers=headers)
    assert r.status_code == 400
