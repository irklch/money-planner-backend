"""E2: гостевая запись устройства, access JWT, ротация refresh, grace, reuse, коды 401, лимиты, 503.

Все ответы сверяются с contract/openapi.yaml (tests/contract.py).
"""

import asyncio
import logging
import uuid
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.security import hash_refresh_token
from app.db.schema import Device, RefreshToken
from app.db.session import sessionmaker
from tests.conftest import auth
from tests.contract import conforms
from tests.test_e1_service import database_down  # noqa: F401  фикстура: БД недоступна

BODY = {"platform": "ios", "appVersion": "1.0.0"}
GUEST = ("post", "/v1/auth/guest")
REFRESH = ("post", "/v1/auth/refresh")
PARSE = ("post", "/v1/imports/parse")


async def create(client, key=None, body=None, **headers):
    r = await client.post(
        "/v1/auth/guest",
        json=BODY if body is None else body,
        headers={"Idempotency-Key": key or str(uuid.uuid4()), **headers},
    )
    conforms(*GUEST, r)
    return r


async def refresh(client, token):
    r = await client.post("/v1/auth/refresh", json={"refreshToken": token})
    conforms(*REFRESH, r)
    return r


async def protected(client, headers):
    """Маршрут с гостевым токеном. Без файла после успешной авторизации — 400 invalid_request."""
    r = await client.post("/v1/imports/parse", headers=headers)
    conforms(*PARSE, r)
    return r


def code(r) -> str:
    return r.json()["error"]["code"]


async def family_rows(family_id) -> list[RefreshToken]:
    async with sessionmaker()() as s:
        return list(await s.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id)))


async def token_row(token: str) -> RefreshToken:
    async with sessionmaker()() as s:
        return await s.get(RefreshToken, hash_refresh_token(token))


# --- создание гостевой записи ---------------------------------------------------------------------
async def test_guest_session_by_contract(client, clock):
    r = await create(client)
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    g = r.json()
    assert g["subjectKind"] == "guest" and uuid.UUID(g["deviceId"])

    settings = get_settings()
    claims = jwt.decode(g["accessToken"], settings.jwt_secret.get_secret_value(), algorithms=["HS256"])
    assert claims["typ"] == "access" and claims["kind"] == "guest" and claims["did"] == g["deviceId"]
    assert claims["iat"] == int(clock.now.timestamp())
    assert claims["exp"] - claims["iat"] == 900  # ACCESS_TOKEN_TTL_S
    assert g["accessTokenExpiresAt"].startswith(
        (clock.now + timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    assert g["refreshTokenExpiresAt"].startswith(
        (clock.now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    )

    async with sessionmaker()() as s:
        device = await s.get(Device, uuid.UUID(g["deviceId"]))
        assert (device.platform, device.app_version, device.account_id) == ("ios", "1.0.0", None)
        assert device.created_at == device.last_seen_at == clock.now
        row = await s.get(RefreshToken, hash_refresh_token(g["refreshToken"]))
    assert (row.subject_kind, row.device_id, row.user_id) == ("guest", device.device_id, None)
    assert (row.rotated_at, row.revoked_at, row.revoked_reason) == (None, None, None)
    assert row.expires_at == clock.now + timedelta(days=365)


async def test_each_installation_gets_its_own_device(client):
    a, b = (await create(client)).json(), (await create(client)).json()
    assert a["deviceId"] != b["deviceId"] and a["refreshToken"] != b["refreshToken"]
    assert len({(await token_row(t["refreshToken"])).family_id for t in (a, b)}) == 2


async def test_client_cannot_choose_device_id(client):
    """deviceId создаёт сервер: чужой id в теле игнорируется, чужая сессия не выдаётся."""
    victim = (await create(client)).json()
    r = await create(client, body={**BODY, "deviceId": victim["deviceId"], "device_id": victim["deviceId"]})
    assert r.status_code == 201 and r.json()["deviceId"] != victim["deviceId"]
    assert r.json()["refreshToken"] != victim["refreshToken"]


@pytest.mark.parametrize(
    ("key", "body", "status", "error", "field"),
    [
        (None, BODY, 400, "invalid_request", None),
        ("not-a-uuid", BODY, 400, "invalid_request", None),
        ("", BODY, 400, "invalid_request", None),
        ("k", [BODY], 400, "invalid_request", None),
        ("k", {"platform": "windows", "appVersion": "1"}, 422, "validation_failed", "platform"),
        ("k", {"appVersion": "1"}, 422, "validation_failed", "platform"),
        ("k", {"platform": "ios", "appVersion": ""}, 422, "validation_failed", "appVersion"),
        ("k", {"platform": "ios", "appVersion": "1" * 33}, 422, "validation_failed", "appVersion"),
        ("k", {"platform": "ios"}, 422, "validation_failed", "appVersion"),
        ("k", {"platform": "ios", "appVersion": 1}, 422, "validation_failed", "appVersion"),
    ],
)
async def test_guest_invalid_input(client, key, body, status, error, field):
    headers = {} if key is None else {"Idempotency-Key": str(uuid.uuid4()) if key == "k" else key}
    r = await client.post("/v1/auth/guest", json=body, headers=headers)
    conforms(*GUEST, r)
    assert r.status_code == status and code(r) == error
    if field:
        assert [d["field"] for d in r.json()["error"]["details"]] == [field]
    async with sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(Device)) == 0


async def test_guest_not_json(client):
    r = await client.post(
        "/v1/auth/guest",
        content=b"{not json",
        headers={"Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
    )
    conforms(*GUEST, r)
    assert r.status_code == 400 and code(r) == "invalid_request"


async def test_legacy_routes_are_gone(client, guest):
    r = await client.post("/v1/auth/anonymous", json=BODY, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 404 and code(r) == "not_found"
    r = await client.delete("/v1/me", headers=auth(guest))
    assert r.status_code == 404 and code(r) == "not_found"
    assert (await refresh(client, guest["refreshToken"])).status_code == 200  # ничего не удалено


# --- access JWT ----------------------------------------------------------------------------------
async def test_access_token_accepted_until_expiry(client, clock):
    g = (await create(client)).json()
    clock.tick(899)
    assert (await protected(client, auth(g))).status_code == 400  # авторизация прошла, нет файла
    clock.tick(1)
    r = await protected(client, auth(g))
    assert r.status_code == 401 and code(r) == "token_expired" and r.json()["error"]["retryable"] is True
    fresh = (await refresh(client, g["refreshToken"])).json()
    assert (await protected(client, auth(fresh))).status_code == 400


def _forge(claims: dict, secret: str | None = None) -> dict:
    token = jwt.encode(claims, secret or get_settings().jwt_secret.get_secret_value(), algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


async def test_access_token_invalid(client, clock):
    g = (await create(client)).json()
    now = int(clock.now.timestamp())
    good = {"typ": "access", "kind": "guest", "did": g["deviceId"], "iat": now, "exp": now + 900}
    cases = [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer garbage"},
        {"Authorization": f"Basic {g['accessToken']}"},
        {"Authorization": f"Bearer {g['refreshToken']}"},  # refresh-токен не access
        _forge(good, secret="another-secret-of-sufficient-length-0123"),  # чужая подпись
        _forge({**good, "exp": now - 10}, secret="another-secret-of-sufficient-length-0123"),
        _forge({**good, "kind": "account", "sub": str(uuid.uuid4())}),  # аккаунт — с E5
        _forge({**good, "typ": "refresh"}),
        _forge({**good, "did": "not-a-uuid"}),
        _forge({k: v for k, v in good.items() if k != "did"}),
        _forge({k: v for k, v in good.items() if k != "kind"}),
        # Старый формат E1: sub = id legacy-пользователя, без kind и did.
        _forge({"sub": str(uuid.uuid4()), "typ": "access", "iat": now, "exp": now + 900}),
        _forge({**good, "did": str(uuid.uuid4())}),  # устройства нет в БД
    ]
    for headers in cases:
        r = await protected(client, headers)
        assert r.status_code == 401 and code(r) == "access_token_invalid", headers
        assert r.json()["error"]["retryable"] is False


async def test_last_seen_updated_at_most_daily(client, clock):
    g = (await create(client)).json()
    did = uuid.UUID(g["deviceId"])

    async def last_seen():
        async with sessionmaker()() as s:
            return await s.scalar(select(Device.last_seen_at).where(Device.device_id == did))

    created = clock.now
    clock.tick(600)
    await protected(client, auth(g))
    assert await last_seen() == created  # меньше суток — без записи
    clock.tick(86_400)
    g2 = (await refresh(client, g["refreshToken"])).json()
    assert await last_seen() == clock.now  # refresh — тоже запрос устройства
    rotated_at = clock.now
    clock.tick(600)
    await refresh(client, g2["refreshToken"])
    assert await last_seen() == rotated_at
    # Любой запрос с access-токеном: отметка старше суток обновляется.
    async with sessionmaker()() as s:
        await s.execute(
            update(Device).where(Device.device_id == did).values(last_seen_at=clock.now - timedelta(days=2))
        )
        await s.commit()
    await protected(client, auth(g2))
    assert await last_seen() == clock.now


# --- refresh: ротация, grace, reuse ----------------------------------------------------------------
async def test_refresh_rotates_within_family(client, clock):
    g = (await create(client)).json()
    clock.tick(30)
    r = await refresh(client, g["refreshToken"])
    assert r.status_code == 200
    new = r.json()
    assert new["subjectKind"] == "guest" and new["deviceId"] == g["deviceId"]
    assert new["refreshToken"] != g["refreshToken"] and new["accessToken"] != g["accessToken"]
    old_row, new_row = await token_row(g["refreshToken"]), await token_row(new["refreshToken"])
    assert old_row.rotated_at == clock.now and old_row.revoked_at is None
    assert new_row.family_id == old_row.family_id and new_row.device_id == old_row.device_id
    assert new_row.expires_at == clock.now + timedelta(days=365)
    assert (await protected(client, auth(new))).status_code == 400


@pytest.mark.parametrize("delay", [0, 59, 60])
async def test_refresh_retry_within_grace_returns_the_same_pair(client, clock, delay):
    g = (await create(client)).json()
    lost = (await refresh(client, g["refreshToken"])).json()
    clock.tick(delay)
    retry = await refresh(client, g["refreshToken"])
    assert retry.status_code == 200 and retry.json() == lost  # та же пара, включая access JWT
    assert len(await family_rows((await token_row(g["refreshToken"])).family_id)) == 2
    assert (await refresh(client, lost["refreshToken"])).status_code == 200


async def test_refresh_retry_beyond_grace_revokes_family(client, clock):
    g = (await create(client)).json()
    lost = (await refresh(client, g["refreshToken"])).json()
    clock.tick(61)
    r = await refresh(client, g["refreshToken"])
    assert r.status_code == 401 and code(r) == "refresh_token_invalid"
    assert g["refreshToken"] not in r.text and lost["refreshToken"] not in r.text
    rows = await family_rows((await token_row(g["refreshToken"])).family_id)
    assert len(rows) == 2 and all(x.revoked_reason == "reuse_detected" and x.revoked_at for x in rows)
    # Пара из потерянного ответа тоже отозвана; гость создаёт новую запись (план §2.8).
    assert code(await refresh(client, lost["refreshToken"])) == "refresh_token_invalid"
    again = (await create(client)).json()
    assert again["deviceId"] != g["deviceId"]


async def test_grace_retry_after_new_token_was_used_is_reuse(client, clock):
    """Новый токен уже использован — значит, ответ не был потерян: повтор старого = reuse."""
    g = (await create(client)).json()
    second = (await refresh(client, g["refreshToken"])).json()
    third = (await refresh(client, second["refreshToken"])).json()
    clock.tick(5)
    assert code(await refresh(client, g["refreshToken"])) == "refresh_token_invalid"
    assert code(await refresh(client, third["refreshToken"])) == "refresh_token_invalid"


async def test_refresh_expired_unknown_and_malformed(client, clock):
    g = (await create(client)).json()
    clock.tick(365 * 86_400)
    r = await refresh(client, g["refreshToken"])
    assert r.status_code == 401 and code(r) == "refresh_token_invalid"
    assert (await token_row(g["refreshToken"])).revoked_at is None  # истечение — не reuse

    assert code(await refresh(client, "rt_" + "x" * 43)) == "refresh_token_invalid"
    for body in (
        {"refreshToken": "short"},
        {"refreshToken": "x" * 257},
        {},
        {"refreshToken": 12345678901234567},
    ):
        r = await client.post("/v1/auth/refresh", json=body)
        conforms(*REFRESH, r)
        assert r.status_code == 422 and code(r) == "validation_failed", body
        assert r.json()["error"]["details"] == [{"code": "refresh_token_malformed", "field": "refreshToken"}]
    r = await client.post("/v1/auth/refresh", content=b"[]", headers={"Content-Type": "application/json"})
    conforms(*REFRESH, r)
    assert r.status_code == 400 and code(r) == "invalid_request"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("reuse_detected", "refresh_token_invalid"),
        ("logout", "refresh_token_invalid"),
        ("session_revoked", "session_revoked"),
        ("account_deleted", "account_deleted"),
    ],
)
async def test_revoked_family_codes(client, clock, reason, expected):
    g = (await create(client)).json()
    family = (await token_row(g["refreshToken"])).family_id
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family)
            .values(revoked_at=clock.now, revoked_reason=reason)
        )
        await s.commit()
    r = await refresh(client, g["refreshToken"])
    assert r.status_code == 401 and code(r) == expected


async def test_devices_are_isolated(client, clock):
    a, b = (await create(client)).json(), (await create(client)).json()
    a2 = (await refresh(client, a["refreshToken"])).json()
    assert a2["deviceId"] == a["deviceId"]
    clock.tick(120)
    assert code(await refresh(client, a["refreshToken"])) == "refresh_token_invalid"  # reuse у A
    b2 = await refresh(client, b["refreshToken"])  # B не задет
    assert b2.status_code == 200 and b2.json()["deviceId"] == b["deviceId"]
    b_rows = await family_rows((await token_row(b["refreshToken"])).family_id)
    assert all(x.revoked_at is None for x in b_rows)


# --- конкурентность --------------------------------------------------------------------------------
async def test_concurrent_refresh_of_one_token_gives_one_rotation(client):
    g = (await create(client)).json()
    rs = await asyncio.gather(*[refresh(client, g["refreshToken"]) for _ in range(8)])
    assert {r.status_code for r in rs} == {200}
    assert len({r.json()["refreshToken"] for r in rs}) == 1
    assert len({r.json()["accessToken"] for r in rs}) == 1
    rows = await family_rows((await token_row(g["refreshToken"])).family_id)
    assert len(rows) == 2 and all(x.revoked_at is None for x in rows)


async def test_concurrent_reuse_and_rotation_leave_no_live_token(client, clock):
    """Отзыв family и параллельная ротация сериализуются: в отозванной family нет действующих токенов."""
    for _ in range(5):
        g = (await create(client)).json()
        second = (await refresh(client, g["refreshToken"])).json()
        clock.tick(61)
        stolen, legit = await asyncio.gather(
            refresh(client, g["refreshToken"]), refresh(client, second["refreshToken"])
        )
        assert code(stolen) == "refresh_token_invalid"
        rows = await family_rows((await token_row(g["refreshToken"])).family_id)
        assert all(x.revoked_reason == "reuse_detected" for x in rows), [x.revoked_reason for x in rows]
        if legit.status_code == 200:
            assert code(await refresh(client, legit.json()["refreshToken"])) == "refresh_token_invalid"
        else:
            assert code(legit) == "refresh_token_invalid"


# --- ограничения частоты -----------------------------------------------------------------------------
class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_guest_create_rate_per_ip(client):
    tick = FakeMonotonic()
    limiter.clock = tick
    limit = get_settings().guest_create_rate_per_ip
    assert limit == 10
    key = str(uuid.uuid4())
    first = await create(client, key=key)
    for i in range(limit - 1):
        tick.t += 1
        # Повтор с тем же ключом тоже учитывается: лимит защищает БД до обращения к ней.
        assert (await create(client, key=key if i == 0 else None)).status_code == 201
    tick.t = 1000.0 + 30
    r = await create(client)
    assert r.status_code == 429 and code(r) == "rate_limited" and r.json()["error"]["retryable"] is True
    assert r.headers["Retry-After"] == "30"  # самый старый запрос выйдет из окна через 30 с
    assert first.json()["refreshToken"] not in r.text
    # Другой IP — отдельный счётчик (за Caddy адрес клиента — X-Forwarded-For).
    assert (await create(client, **{"X-Forwarded-For": "203.0.113.7"})).status_code == 201
    tick.t = 1000.0 + 59.9
    assert (await create(client)).status_code == 429
    tick.t = 1000.0 + 60  # граница окна: первый запрос вышел
    assert (await create(client)).status_code == 201
    assert (await create(client)).status_code == 429
    async with sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(Device)) == limit + 1


async def test_refresh_rate_per_ip(client):
    tick = FakeMonotonic()
    limiter.clock = tick
    g = (await create(client)).json()
    limit = get_settings().rate_auth_per_minute * 3
    for _ in range(limit):
        assert (await refresh(client, "rt_" + "y" * 43)).status_code == 401
    r = await refresh(client, g["refreshToken"])
    assert r.status_code == 429 and code(r) == "rate_limited" and int(r.headers["Retry-After"]) == 60
    tick.t += 60
    assert (await refresh(client, g["refreshToken"])).status_code == 200


# --- недоступная БД и секреты -------------------------------------------------------------------------
async def test_auth_returns_503_when_database_down(client, database_down, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    key = str(uuid.uuid4())
    token = "rt_" + "s3cr3t" * 8
    for r, op in [
        (await client.post("/v1/auth/guest", json=BODY, headers={"Idempotency-Key": key}), GUEST),
        (await client.post("/v1/auth/refresh", json={"refreshToken": token}), REFRESH),
    ]:
        conforms(*op, r)
        assert r.status_code == 503 and code(r) == "service_unavailable"
        assert r.headers["Retry-After"] == str(get_settings().db_unavailable_retry_after_s)
        for leaked in (key, token, "s3cr3t", "127.0.0.1", "money_down"):
            assert leaked not in r.text and leaked not in caplog.text


async def test_protected_route_returns_503_when_database_down(client, clock, database_down):  # noqa: F811
    now = int(clock.now.timestamp())
    headers = _forge(
        {"typ": "access", "kind": "guest", "did": str(uuid.uuid4()), "iat": now, "exp": now + 900}
    )
    r = await protected(client, headers)
    assert r.status_code == 503 and code(r) == "service_unavailable" and "Retry-After" in r.headers


async def test_no_tokens_or_keys_in_database_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    key = str(uuid.uuid4())
    g = (await create(client, key=key)).json()
    replay = (await create(client, key=key)).json()
    r2 = (await refresh(client, g["refreshToken"])).json()
    await refresh(client, g["refreshToken"])  # grace
    await protected(client, auth(r2))
    secrets = {
        key,
        g["refreshToken"],
        g["accessToken"],
        replay["accessToken"],
        r2["refreshToken"],
        r2["accessToken"],
    }
    async with sessionmaker()() as s:
        dump = []
        for table in ("devices", "refresh_tokens", "rate_counters"):
            dump += [str(v) for row in (await s.execute(text(f"SELECT * FROM {table}"))).all() for v in row]
    blob = " ".join(dump)
    for secret in secrets:
        assert secret not in blob
        assert secret not in caplog.text
    assert hash_refresh_token(g["refreshToken"]) in blob and hash_refresh_token(r2["refreshToken"]) in blob
    assert "authorization" not in caplog.text.lower() and "bearer" not in caplog.text.lower()
