import uuid

from tests.conftest import OTHER, PRODUCTS, add_expense


async def create(client, headers, name, emoji="🐶", key=None):
    return await client.post(
        "/v1/categories",
        json={"name": name, "emoji": emoji},
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def test_list_system_categories_other_last(client, headers):
    r = await client.get("/v1/categories", headers=headers)
    items = r.json()["items"]
    assert items[-1]["name"] == "Другое" and items[-1]["kind"] == "system"
    assert all(c["archivedAt"] is None for c in items)


async def test_create_idempotent(client, headers):
    key = str(uuid.uuid4())
    r1 = await create(client, headers, "Питомцы", key=key)
    r2 = await create(client, headers, "Питомцы", key=key)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] == key
    assert r1.json()["kind"] == "custom"
    names = [c["name"] for c in (await client.get("/v1/categories", headers=headers)).json()["items"]]
    assert names.count("Питомцы") == 1


async def test_name_unique_case_insensitive_incl_system(client, headers):
    r = await create(client, headers, "  продукты ")
    assert r.status_code == 409 and r.json()["error"]["code"] == "category_name_taken"
    assert (await create(client, headers, "Питомцы")).status_code == 201
    r = await create(client, headers, "ПИТОМЦЫ")
    assert r.json()["error"]["code"] == "category_name_taken"


async def test_validation(client, headers):
    r = await create(client, headers, "", "🐶")
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "name_required"
    r = await create(client, headers, "x" * 25)
    assert r.json()["error"]["details"][0]["code"] == "name_too_long"
    r = await create(client, headers, "Ок", "ab")
    assert r.json()["error"]["details"][0]["code"] == "emoji_invalid"
    r = await create(client, headers, "Ок", "🐶🐱")
    assert r.json()["error"]["details"][0]["code"] == "emoji_invalid"
    r = await create(client, headers, "Ок", "👨‍👩‍👧")  # одна графема из нескольких code points
    assert r.status_code == 201


async def test_system_readonly(client, headers):
    r = await client.patch(f"/v1/categories/{OTHER}", json={"name": "X"}, headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "category_readonly"
    r = await client.delete(f"/v1/categories/{OTHER}", headers=headers)
    assert r.status_code == 403


async def test_archive_flow(client, headers):
    cat = (await create(client, headers, "Питомцы")).json()
    exp = await add_expense(client, headers, "2026-09-10", "500.00", cat["id"])

    r = await client.delete(f"/v1/categories/{cat['id']}", headers=headers)
    assert r.status_code == 200 and r.json()["archivedAt"] is not None
    r2 = await client.delete(f"/v1/categories/{cat['id']}", headers=headers)
    assert r2.status_code == 200 and r2.json()["archivedAt"] == r.json()["archivedAt"]

    default = [c["id"] for c in (await client.get("/v1/categories", headers=headers)).json()["items"]]
    assert cat["id"] not in default
    full = (await client.get("/v1/categories?includeArchived=true", headers=headers)).json()["items"]
    assert cat["id"] in [c["id"] for c in full]

    # Расход остался со своей категорией, аналитика её показывает.
    s = (await client.get("/v1/analytics/summary?from=2026-09-01&to=2026-09-30", headers=headers)).json()
    assert s["categories"][0]["categoryId"] == cat["id"]

    # Новый расход с архивной — нельзя; PATCH существующего с той же категорией — можно.
    r = await client.post(
        "/v1/expenses",
        json={"date": "2026-09-11", "amount": "1.00", "categoryId": cat["id"]},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "category_archived"
    r = await client.patch(
        f"/v1/expenses/{exp['id']}", json={"amount": "600.00", "categoryId": cat["id"]}, headers=headers
    )
    assert r.status_code == 200 and r.json()["categoryId"] == cat["id"]
    # После смены на активную вернуть архивную нельзя.
    await client.patch(f"/v1/expenses/{exp['id']}", json={"categoryId": PRODUCTS}, headers=headers)
    r = await client.patch(f"/v1/expenses/{exp['id']}", json={"categoryId": cat["id"]}, headers=headers)
    assert r.status_code == 422

    # Архивную нельзя переименовать; имя архивной можно занять снова (новый UUID).
    r = await client.patch(f"/v1/categories/{cat['id']}", json={"name": "Y"}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "category_archived"
    r = await create(client, headers, "Питомцы")
    assert r.status_code == 201 and r.json()["id"] != cat["id"]
