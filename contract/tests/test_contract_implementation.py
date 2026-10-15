"""Контракт ↔ код по этапам: `x-contract.implementedThrough` — последний реализованный этап.

- Эндпоинты с `x-stage` не позже него обязаны быть смонтированы приложением.
- Эндпоинты более поздних этапов реализованными не считаются. Если путь уже обслуживает старый код
  (до замены на месте, план §11.2), он обязан быть в LEGACY с этапом замены — это не реализация контракта.
- Ответы реализованных эндпоинтов проверяются против схем контракта тестами этапа (tests/test_e1_service.py,
  tests/test_auth.py, tests/test_guest_replay.py, tests/test_e2_contract_scenarios.py).
"""

import re

from httpx import ASGITransport, AsyncClient

# Старые маршруты на месте эндпоинтов контракта: (метод, путь) → этап, который их заменит.
# E2: старый разбор принимает гостевой токен E2, но реализацией контракта E3 не считается.
LEGACY = {
    ("post", "/v1/imports/parse"): "E3",
}


def _stage(value: str) -> int:
    return int(re.fullmatch(r"E(\d+)", value).group(1))


def _app_routes() -> set[tuple[str, str]]:
    from app.main import create_app

    # Все маршруты /v1 входят в схему приложения; /healthz проверяется запросом (он вне схемы).
    return {(method, path) for path, ops in create_app().openapi()["paths"].items() for method in ops}


def test_implemented_through_is_a_stage(spec):
    assert _stage(spec["x-contract"]["implementedThrough"]) >= 1


def test_implemented_through_is_e2(spec, operations):
    """E2 реализован (/auth/guest, /auth/refresh), следующие этапы — нет (/auth/apple, /me)."""
    assert spec["x-contract"]["implementedThrough"] == "E2"
    assert operations[("post", "/v1/auth/guest")]["x-stage"] == "E2"
    assert operations[("post", "/v1/auth/refresh")]["x-stage"] == "E2"
    routed = _app_routes()
    assert {("post", "/v1/auth/guest"), ("post", "/v1/auth/refresh")} <= routed
    assert not {("post", "/v1/auth/apple"), ("post", "/v1/auth/logout"), ("get", "/v1/me")} & routed


async def test_due_operations_are_routed(spec, operations):
    from app.main import create_app

    through = _stage(spec["x-contract"]["implementedThrough"])
    due = [(m, p) for (m, p), op in operations.items() if _stage(op["x-stage"]) <= through]
    assert ("get", "/healthz") in due
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        for method, path in due:
            concrete = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)
            r = await client.request(method.upper(), concrete)
            # 404/405 от маршрутизатора — эндпоинт не реализован (503 без БД — реализован).
            assert r.status_code not in (404, 405), (method, path, r.status_code)


def test_later_stage_paths_served_by_old_code_are_declared_legacy(spec, operations):
    through = _stage(spec["x-contract"]["implementedThrough"])
    routed = _app_routes()
    later = {key: op["x-stage"] for key, op in operations.items() if _stage(op["x-stage"]) > through}
    served_by_old_code = {key: stage for key, stage in later.items() if key in routed}
    assert served_by_old_code == LEGACY
