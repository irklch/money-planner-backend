from tests.conftest import CAFE, PRODUCTS, add_expense


async def test_averages_by_accounted_days(client, headers):
    # Пн 2026-09-07 … Вс 2026-09-13
    await add_expense(client, headers, "2026-09-07", "100.00", PRODUCTS)
    await add_expense(client, headers, "2026-09-08", "300.00", CAFE)
    await client.put("/v1/calendar/acknowledgements/2026-09-09", headers=headers)  # 0 ₽, учтён
    await add_expense(client, headers, "2026-09-12", "200.00", PRODUCTS)  # суббота
    # 10, 11, 13 — не учтены
    r = await client.get("/v1/analytics/dynamics?from=2026-09-07&to=2026-09-13", headers=headers)
    d = r.json()
    assert len(d["points"]) == 7 and d["points"][2] == {"date": "2026-09-09", "total": "0.00"}
    assert d["averages"]["accountedDays"] == 4 and d["averages"]["perDay"] == "150.00"
    assert d["averages"]["weekday"] == {"accountedDays": 3, "perDay": "133.33"}
    assert d["averages"]["weekend"] == {"accountedDays": 1, "perDay": "200.00"}

    # Фильтр по категории: учтённость дней остаётся глобальной.
    r = await client.get(
        f"/v1/analytics/dynamics?from=2026-09-07&to=2026-09-13&categoryId={CAFE}", headers=headers
    )
    a = r.json()["averages"]
    assert a["accountedDays"] == 4 and a["perDay"] == "75.00"


async def test_no_accounted_days_per_day_null(client, headers):
    r = await client.get("/v1/analytics/dynamics?from=2026-09-01&to=2026-09-03", headers=headers)
    assert r.json()["averages"]["perDay"] is None


async def test_summary_with_comparison(client, headers):
    await add_expense(client, headers, "2026-08-05", "100.00", CAFE)
    await add_expense(client, headers, "2026-09-05", "150.00", CAFE)
    await add_expense(client, headers, "2026-09-06", "50.00", PRODUCTS)
    await add_expense(client, headers, "2026-09-06", "50.00", PRODUCTS)
    r = await client.get(
        "/v1/analytics/summary?from=2026-09-01&to=2026-09-30&compareFrom=2026-08-01&compareTo=2026-08-31",
        headers=headers,
    )
    s = r.json()
    assert s["total"] == "250.00" and s["previous"] == {"total": "100.00"}
    cafe, products = s["categories"]
    assert cafe["categoryId"] == CAFE and cafe["share"] == 0.6 and cafe["delta"] == "50.00"
    assert cafe["deltaPercent"] == 50.0 and cafe["averageExpense"] == "150.00"
    assert products["expenseCount"] == 2 and products["averageExpense"] == "50.00"
    assert products["delta"] == "100.00" and products["deltaPercent"] is None  # 0 в прошлом периоде
    assert s["dataRange"] == {"firstDate": "2026-08-05", "lastDate": "2026-09-06"}


async def test_previous_null_without_accounted_days(client, headers):
    await add_expense(client, headers, "2026-09-05", "10.00")
    url = "/v1/analytics/summary?from=2026-09-01&to=2026-09-30&compareFrom=2026-08-01&compareTo=2026-08-31"
    s = (await client.get(url, headers=headers)).json()
    assert s["previous"] is None and s["categories"][0]["delta"] is None
    # «Бесплатный день» делает период сравнения учтённым → previous.total = 0 валиден.
    await client.put("/v1/calendar/acknowledgements/2026-08-10", headers=headers)
    s = (await client.get(url, headers=headers)).json()
    assert s["previous"] == {"total": "0.00"}


async def test_range_errors(client, headers):
    r = await client.get("/v1/analytics/summary?from=2026-09-30&to=2026-09-01", headers=headers)
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "range_invalid"
    r = await client.get(
        "/v1/analytics/summary?from=2026-09-01&to=2026-09-30&compareFrom=2026-08-01", headers=headers
    )
    assert r.status_code == 422
