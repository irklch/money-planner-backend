"""E1: /healthz по контракту, 503 при недоступной БД, состав маршрутов после отключения модулей."""

import pathlib
import sys

import pytest

from app.core.config import Settings, get_settings
from app.db import session as db_session
from app.main import app

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import contract_spec  # noqa: E402

SPEC = contract_spec.load_yaml(ROOT / "contract" / "openapi.yaml")


def conforms(pointer: str, body) -> None:
    errors = [e.message for e in contract_spec.schema_validator(SPEC, pointer).iter_errors(body)]
    assert not errors, errors


@pytest.fixture
async def database_down():
    """Пул указывает на закрытый порт: так выглядит остановленная или недоступная БД."""
    saved = db_session._engine, db_session._sessionmaker
    db_session._engine = None
    db_session._sessionmaker = None
    db_session.init_engine(
        Settings(database_url="postgresql+asyncpg://money:s3cr3t-pw@127.0.0.1:9/money_down", db_pool_size=1)
    )
    yield
    if db_session._engine is not None and db_session._engine is not saved[0]:
        await db_session._engine.dispose()
    db_session._engine, db_session._sessionmaker = saved


async def test_healthz_ok_when_database_answers(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    conforms("/components/schemas/Health", r.json())
    assert r.headers["X-Request-Id"].startswith("req_")


async def test_healthz_503_with_retry_after_when_database_down(client, database_down, caplog):
    r = await client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    conforms("/components/schemas/ErrorEnvelope", body)
    assert body["error"]["code"] == "service_unavailable" and body["error"]["retryable"] is True
    assert r.headers["Retry-After"] == str(get_settings().db_unavailable_retry_after_s)
    # Ни адреса, ни имени БД, ни пароля, ни текста драйвера — ни в ответе, ни в логе.
    for leaked in ("127.0.0.1", ":9", "money_down", "s3cr3t", "Connect", "refused"):
        assert leaked not in r.text
        assert leaked not in caplog.text


async def test_database_route_returns_503_not_500_when_database_down(client, database_down):
    # Любой маршрут, которому нужна БД, отвечает контрактным 503, а не internal_error.
    r = await client.post("/v1/auth/refresh", json={"refreshToken": "x" * 43})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"
    assert "Retry-After" in r.headers


async def test_healthz_recovers_after_database_returns(client, database_down):
    assert (await client.get("/healthz")).status_code == 503
    db_session._engine = None
    db_session._sessionmaker = None
    db_session.init_engine()
    assert (await client.get("/healthz")).status_code == 200


# Отключено в E1 (план §11.2): логика перешла на iOS или вне MVP. Код модулей удаляется в E11.
DISABLED = [
    ("GET", "/v1/categories"),
    ("POST", "/v1/categories"),
    ("PATCH", "/v1/categories/00000000-0000-4000-8000-000000000001"),
    ("GET", "/v1/expenses?from=2026-09-01&to=2026-09-30"),
    ("POST", "/v1/expenses"),
    ("POST", "/v1/expenses/import"),
    ("PATCH", "/v1/expenses/00000000-0000-4000-8000-000000000001"),
    ("DELETE", "/v1/expenses/00000000-0000-4000-8000-000000000001"),
    ("GET", "/v1/calendar/days?from=2026-09-01&to=2026-09-30"),
    ("PUT", "/v1/calendar/acknowledgements/2026-09-01"),
    ("GET", "/v1/analytics/summary?from=2026-09-01&to=2026-09-30"),
    ("GET", "/v1/analytics/dynamics?from=2026-09-01&to=2026-09-30"),
    ("GET", "/v1/insights?from=2026-09-01&to=2026-09-30"),
]


@pytest.mark.parametrize(("method", "path"), DISABLED)
async def test_disabled_routes_are_gone(client, guest, method, path):
    r = await client.request(method, path, headers={"Authorization": f"Bearer {guest['accessToken']}"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"


async def test_route_table_is_exactly_e1(client):
    # Маршруты API — из схемы приложения (все маршруты /v1 в неё входят); /healthz вне схемы.
    api = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    # Старые маршруты остаются до замены: /auth/* → E2 (/auth/guest), DELETE /me → E7, parse → E3.
    assert api == {
        ("POST", "/v1/auth/anonymous"),
        ("POST", "/v1/auth/refresh"),
        ("DELETE", "/v1/me"),
        ("POST", "/v1/imports/parse"),
    }
    assert (await client.get("/healthz")).status_code == 200
