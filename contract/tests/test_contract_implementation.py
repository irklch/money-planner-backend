"""Контракт ↔ код: эндпоинты этапов до `x-contract.implementedThrough` реализованы приложением.

Начиная с E1 каждый этап поднимает `implementedThrough` и тем самым требует свои эндпоинты.
Ответы реализованных эндпоинтов проверяются против схем контракта тестами этапа (tests_ydb).
"""

import re

from httpx import ASGITransport, AsyncClient


def _stage(value: str) -> int:
    return int(re.fullmatch(r"E(\d+)", value).group(1))


def test_implemented_through_is_a_stage(spec):
    assert _stage(spec["x-contract"]["implementedThrough"]) >= 1


async def test_due_operations_are_routed(spec, operations):
    from app.main import create_app

    through = _stage(spec["x-contract"]["implementedThrough"])
    due = [(m, p) for (m, p), op in operations.items() if _stage(op["x-stage"]) <= through]
    assert ("get", "/healthz") in due
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in due:
            concrete = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)
            r = await client.request(method.upper(), concrete)
            # 404/405 от маршрутизатора — эндпоинт не реализован.
            assert r.status_code not in (404, 405), (method, path, r.status_code)
