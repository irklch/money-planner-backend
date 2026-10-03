import uuid
from datetime import date, timedelta

from tests.conftest import CAFE, PRODUCTS, add_expense


async def days(client, headers, frm, to):
    r = await client.get(f"/v1/calendar/days?from={frm}&to={to}", headers=headers)
    assert r.status_code == 200, r.text
    return {d["date"]: d for d in r.json()["days"]}


async def test_create_validation(client, headers):
    future = (date.today() + timedelta(days=3)).isoformat()
    cases = [
        ({"date": "2026-09-01", "amount": "0", "categoryId": PRODUCTS}, "amount_invalid"),
        ({"date": "2026-09-01", "amount": "1.234", "categoryId": PRODUCTS}, "amount_invalid"),
        ({"date": "2026-09-01", "amount": 10.5, "categoryId": PRODUCTS}, "amount_invalid"),
        ({"date": "2026-09-01", "amount": "1000000000.00", "categoryId": PRODUCTS}, "amount_too_large"),
        ({"date": "01.09.2026", "amount": "1", "categoryId": PRODUCTS}, "date_invalid"),
        ({"date": future, "amount": "1", "categoryId": PRODUCTS}, "date_in_future"),
        (
            {"date": "2026-09-01", "amount": "1", "categoryId": PRODUCTS, "comment": "x" * 201},
            "comment_too_long",
        ),
        ({"date": "2026-09-01", "amount": "1", "categoryId": str(uuid.uuid4())}, "category_not_found"),
    ]
    for body, code in cases:
        r = await client.post(
            "/v1/expenses", json=body, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        assert r.status_code == 422, (body, r.text)
        assert code in [d["code"] for d in r.json()["error"]["details"]], (body, r.json())


async def test_create_idempotent(client, headers):
    key = str(uuid.uuid4())
    body = {"date": "2026-09-01", "amount": "99.90", "categoryId": PRODUCTS, "comment": "ВКУСВИЛЛ"}
    r1 = await client.post("/v1/expenses", json=body, headers={**headers, "Idempotency-Key": key})
    r2 = await client.post("/v1/expenses", json=body, headers={**headers, "Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["amount"] == "99.90" and r1.json()["source"] == "manual"
    r = await client.get("/v1/expenses?from=2026-09-01&to=2026-09-01", headers=headers)
    assert len(r.json()["items"]) == 1


async def test_patch_delete(client, headers):
    e = await add_expense(client, headers, "2026-09-02", "10.00")
    r = await client.patch(f"/v1/expenses/{e['id']}", json={"comment": None, "amount": "20"}, headers=headers)
    assert r.status_code == 200 and r.json()["amount"] == "20.00" and r.json()["comment"] is None
    assert (await client.delete(f"/v1/expenses/{e['id']}", headers=headers)).status_code == 204
    r = await client.delete(f"/v1/expenses/{e['id']}", headers=headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "expense_not_found"


async def test_pagination(client, headers):
    for i in range(5):
        await add_expense(client, headers, f"2026-09-0{i + 1}", "1.00")
    seen, cursor = [], None
    while True:
        url = "/v1/expenses?from=2026-09-01&to=2026-09-30&limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url, headers=headers)).json()
        seen += [x["date"] for x in page["items"]]
        cursor = page["nextCursor"]
        if not cursor:
            break
    assert seen == sorted(seen, reverse=True) and len(seen) == 5


async def test_free_day_xor_expense(client, headers):
    d = "2026-09-05"
    r = await client.put(f"/v1/calendar/acknowledgements/{d}", headers=headers)
    assert (
        r.status_code == 200 and r.json()["isAcknowledgedEmpty"] is True and r.json()["isAccounted"] is True
    )
    assert (await client.put(f"/v1/calendar/acknowledgements/{d}", headers=headers)).status_code == 200

    await add_expense(client, headers, d)  # первый расход снимает «Бесплатный день»
    day = (await days(client, headers, d, d))[d]
    assert day == {"date": d, "expenseCount": 1, "isAcknowledgedEmpty": False, "isAccounted": True}

    r = await client.put(f"/v1/calendar/acknowledgements/{d}", headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "day_has_expenses"


async def test_patch_date_onto_free_day_clears_it(client, headers):
    e = await add_expense(client, headers, "2026-09-06")
    await client.put("/v1/calendar/acknowledgements/2026-09-07", headers=headers)
    await client.patch(f"/v1/expenses/{e['id']}", json={"date": "2026-09-07"}, headers=headers)
    cal = await days(client, headers, "2026-09-06", "2026-09-07")
    assert cal["2026-09-07"]["isAcknowledgedEmpty"] is False and cal["2026-09-07"]["expenseCount"] == 1
    assert cal["2026-09-06"]["isAccounted"] is False


async def test_unacknowledge_and_ranges(client, headers):
    await client.put("/v1/calendar/acknowledgements/2026-09-08", headers=headers)
    r = await client.delete("/v1/calendar/acknowledgements/2026-09-08", headers=headers)
    assert r.json()["isAccounted"] is False
    assert (
        await client.delete("/v1/calendar/acknowledgements/2026-09-08", headers=headers)
    ).status_code == 200
    r = await client.get("/v1/calendar/days?from=2026-01-01&to=2027-01-02", headers=headers)
    assert r.json()["error"]["details"][0]["code"] == "range_too_large"
    r = await client.get("/v1/calendar/days?from=2026-02-01&to=2026-01-01", headers=headers)
    assert r.json()["error"]["details"][0]["code"] == "range_invalid"
    future = (date.today() + timedelta(days=3)).isoformat()
    r = await client.put(f"/v1/calendar/acknowledgements/{future}", headers=headers)
    assert r.json()["error"]["details"][0]["code"] == "date_in_future"
    cal = await days(client, headers, "2026-09-01", "2026-09-30")
    assert len(cal) == 30


async def test_other_categories_used(client, headers):
    await add_expense(client, headers, "2026-09-09", "5.00", CAFE)
    r = await client.get(f"/v1/expenses?from=2026-09-01&to=2026-09-30&categoryId={CAFE}", headers=headers)
    assert len(r.json()["items"]) == 1
