"""HTTP-поведение слоя YDB: /healthz по контракту E0, 503 + Retry-After, учёт RU в access-логе."""

import logging

import contract_spec
import pytest
from httpx import ASGITransport, AsyncClient

from app.db_ydb.errors import YdbUnavailable
from app.main import create_app
from tests_ydb.conftest import ROOT

SPEC = contract_spec.load_yaml(ROOT / "contract" / "openapi.yaml")


def conforms(path: str, method: str, resp) -> None:
    """Ответ приложения соответствует контракту E0 (статус, заголовки, схема тела, код ошибки)."""
    op = contract_spec.operations(SPEC)[(method, path)]
    declared = contract_spec.resolve(SPEC, op["responses"][str(resp.status_code)])
    for name, h in declared.get("headers", {}).items():
        if contract_spec.resolve(SPEC, h).get("required"):
            assert name in resp.headers, name
    if "x-error-codes" in declared:
        assert resp.json()["error"]["code"] in declared["x-error-codes"]
    pointer = declared["content"]["application/json"]["schema"]["$ref"][1:]
    errors = list(contract_spec.schema_validator(SPEC, pointer).iter_errors(resp.json()))
    assert not errors, [e.message for e in errors]


class FakeYdb:
    def __init__(self, error=None):
        self.error = error
        self.pings = 0

    async def ping(self):
        self.pings += 1
        if self.error:
            raise self.error


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


async def test_healthz_without_ydb_configured():
    app = create_app()
    r = await _get(app, "/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    conforms("/healthz", "get", r)


async def test_healthz_ok_with_ydb():
    app = create_app()
    app.state.ydb = FakeYdb()
    r = await _get(app, "/healthz")
    assert r.status_code == 200 and app.state.ydb.pings == 1
    conforms("/healthz", "get", r)


async def test_healthz_ydb_unavailable_is_503_with_retry_after():
    app = create_app()
    app.state.ydb = FakeYdb(YdbUnavailable("Overloaded", retry_after=3))
    r = await _get(app, "/healthz")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "3"
    body = r.json()["error"]
    assert (body["code"], body["retryable"]) == ("service_unavailable", True)
    assert body["requestId"] == r.headers["X-Request-Id"]
    assert "Overloaded" not in r.text  # причина — только в логах
    conforms("/healthz", "get", r)


async def test_unexpected_ydb_error_is_500_without_details():
    import ydb

    app = create_app()
    app.state.ydb = FakeYdb(ydb.issues.SchemeError("table mp/secret not found"))
    r = await _get(app, "/healthz")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "secret" not in r.text


DISABLED = (
    ("get", "/v1/expenses"),
    ("get", "/v1/categories"),
    ("get", "/v1/calendar/days"),
    ("get", "/v1/analytics/summary"),
    ("get", "/v1/insights"),
    ("post", "/v1/expenses/import"),
)


async def test_legacy_routes_disabled():
    """План §11.2: отключённые модули не смонтированы; оставшиеся маршруты на месте."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for method, path in DISABLED:
            r = await c.request(method.upper(), path)
            assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    paths = set(app.openapi()["paths"])
    assert paths == {"/v1/auth/anonymous", "/v1/auth/refresh", "/v1/me", "/v1/imports/parse"}


@pytest.mark.ydb
async def test_healthz_real_ydb_logs_ru(migrated, caplog):
    app = create_app()
    app.state.ydb = migrated
    with caplog.at_level(logging.INFO, logger="app.http"):
        r = await _get(app, "/healthz")
    assert r.status_code == 200
    conforms("/healthz", "get", r)
    rec = next(x for x in caplog.records if x.getMessage() == "request")
    assert rec.ydb_calls >= 1 and rec.ydb_ru >= 0
