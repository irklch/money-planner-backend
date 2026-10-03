import uuid

from tests.conftest import add_expense, auth, new_guest


async def test_user_cannot_touch_foreign_data(client):
    a = auth(await new_guest(client))
    b = auth(await new_guest(client))

    cat = (
        await client.post(
            "/v1/categories",
            json={"name": "Секрет", "emoji": "🔒"},
            headers={**a, "Idempotency-Key": str(uuid.uuid4())},
        )
    ).json()
    exp = await add_expense(client, a, "2026-09-01", "777.00", cat["id"], "личное")
    await client.put("/v1/calendar/acknowledgements/2026-09-02", headers=a)

    # Чтение
    assert (await client.get("/v1/expenses?from=2026-09-01&to=2026-09-30", headers=b)).json()["items"] == []
    cats = (await client.get("/v1/categories?includeArchived=true", headers=b)).json()["items"]
    assert cat["id"] not in [c["id"] for c in cats]
    s = (await client.get("/v1/analytics/summary?from=2026-09-01&to=2026-09-30", headers=b)).json()
    assert s["total"] == "0.00" and s["dataRange"] is None
    days = (await client.get("/v1/calendar/days?from=2026-09-01&to=2026-09-02", headers=b)).json()["days"]
    assert not any(d["isAccounted"] for d in days)

    # Запись
    r = await client.patch(f"/v1/expenses/{exp['id']}", json={"amount": "1"}, headers=b)
    assert r.status_code == 404 and r.json()["error"]["code"] == "expense_not_found"
    assert (await client.delete(f"/v1/expenses/{exp['id']}", headers=b)).status_code == 404
    r = await client.patch(f"/v1/categories/{cat['id']}", json={"name": "Взлом"}, headers=b)
    assert r.status_code == 404 and r.json()["error"]["code"] == "category_not_found"
    assert (await client.delete(f"/v1/categories/{cat['id']}", headers=b)).status_code == 404
    r = await client.post(
        "/v1/expenses",
        json={"date": "2026-09-03", "amount": "1", "categoryId": cat["id"]},
        headers={**b, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.json()["error"]["details"][0]["code"] == "category_not_found"
    r = await client.post(
        "/v1/expenses/import",
        json={"expenses": [{"date": "2026-09-03", "amount": "1", "categoryId": cat["id"]}]},
        headers={**b, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422
    # Чужой Idempotency-Key (id расхода A) не отдаёт чужой расход.
    r = await client.post(
        "/v1/expenses",
        json={"date": "2026-09-03", "amount": "1", "categoryId": "00000000-0000-4000-8000-000000000001"},
        headers={**b, "Idempotency-Key": exp["id"]},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert "777" not in r.text

    # У A всё на месте
    items = (await client.get("/v1/expenses?from=2026-09-01&to=2026-09-30", headers=a)).json()["items"]
    assert items[0]["amount"] == "777.00"

    # DELETE /me пользователя B не трогает данные A
    await client.delete("/v1/me", headers=b)
    assert (
        len((await client.get("/v1/expenses?from=2026-09-01&to=2026-09-30", headers=a)).json()["items"]) == 1
    )
