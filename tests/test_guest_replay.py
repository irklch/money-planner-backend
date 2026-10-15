"""E2: повтор POST /v1/auth/guest с тем же Idempotency-Key (контракт, docs/api-contract.md §5)."""

import asyncio
import uuid

from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.core.security import derive_refresh_token, hash_refresh_token
from app.db.schema import Device, RefreshToken
from app.db.session import sessionmaker
from tests.contract import conforms

BODY = {"platform": "ios", "appVersion": "1.0"}


async def guest(client, key, body=BODY):
    r = await client.post("/v1/auth/guest", json=body, headers={"Idempotency-Key": key})
    conforms("post", "/v1/auth/guest", r)
    return r


async def counts():
    async with sessionmaker()() as s:
        return (
            await s.scalar(select(func.count()).select_from(Device)),
            await s.scalar(select(func.count()).select_from(RefreshToken)),
        )


async def test_replay_returns_same_device_and_refresh_token(client, clock):
    key = str(uuid.uuid4())
    first = (await guest(client, key)).json()
    clock.tick(30)
    replay = await guest(client, key)
    assert replay.status_code == 201 and replay.headers["Idempotent-Replayed"] == "true"
    second = replay.json()
    # Тот же deviceId, refresh-токен и его срок; access JWT — новый (контракт).
    for f in ("subjectKind", "deviceId", "refreshToken", "refreshTokenExpiresAt"):
        assert first[f] == second[f]
    assert first["accessToken"] != second["accessToken"]
    assert await counts() == (1, 1)  # ни второго устройства, ни второй строки токена

    # Токен восстанавливается вычислением, а не чтением из БД; ключа и токена в БД нет.
    token = derive_refresh_token(get_settings(), "guest-key", key)
    assert token == first["refreshToken"]
    async with sessionmaker()() as s:
        row = (await s.execute(text("SELECT * FROM refresh_tokens"))).mappings().one()
        dump = " ".join(str(v) for v in row.values())
    assert row["token_hash"] == hash_refresh_token(token)
    assert token not in dump and key not in dump


async def test_replay_after_rotation_is_409_without_tokens(client):
    key = str(uuid.uuid4())
    first = (await guest(client, key)).json()
    rotated = await client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]})
    assert rotated.status_code == 200

    r = await guest(client, key)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert "Idempotent-Replayed" not in r.headers
    assert first["refreshToken"] not in r.text and rotated.json()["refreshToken"] not in r.text
    assert await counts() == (1, 2)  # нового устройства нет
    ok = await client.post("/v1/auth/refresh", json={"refreshToken": rotated.json()["refreshToken"]})
    assert ok.status_code == 200  # действующая сессия не задета


async def test_replay_after_revocation_is_409(client, clock):
    key = str(uuid.uuid4())
    first = (await guest(client, key)).json()
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(first["refreshToken"]))
            .values(revoked_at=clock.now, revoked_reason="reuse_detected")
        )
        await s.commit()
    r = await guest(client, key)
    assert r.status_code == 409 and first["refreshToken"] not in r.text


async def test_replay_window_is_24_hours(client, clock):
    key = str(uuid.uuid4())
    first = (await guest(client, key)).json()
    clock.tick(24 * 3600)
    assert (await guest(client, key)).json()["refreshToken"] == first["refreshToken"]  # ровно 24 ч — ещё да
    clock.tick(1)
    r = await guest(client, key)
    assert r.status_code == 409 and first["refreshToken"] not in r.text
    assert await counts() == (1, 1)


async def test_same_key_with_another_request_is_409(client):
    key = str(uuid.uuid4())
    first = (await guest(client, key)).json()
    for body in ({"platform": "android", "appVersion": "1.0"}, {"platform": "ios", "appVersion": "2.0"}):
        r = await guest(client, key, body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
        assert first["refreshToken"] not in r.text
    assert (await guest(client, key)).status_code == 201  # исходный запрос по-прежнему повторяем


async def test_parallel_first_requests_create_one_device(client):
    key = str(uuid.uuid4())
    rs = await asyncio.gather(*[guest(client, key) for _ in range(8)])
    assert {r.status_code for r in rs} == {201}
    assert len({r.json()["deviceId"] for r in rs}) == 1
    assert len({r.json()["refreshToken"] for r in rs}) == 1
    assert sum("Idempotent-Replayed" not in r.headers for r in rs) == 1  # создала запись одна
    assert await counts() == (1, 1)


async def test_parallel_different_keys_create_distinct_devices(client):
    rs = await asyncio.gather(*[guest(client, str(uuid.uuid4())) for _ in range(8)])
    assert {r.status_code for r in rs} == {201}
    assert len({r.json()["deviceId"] for r in rs}) == 8
    assert await counts() == (8, 8)


async def test_replay_racing_with_refresh_never_returns_a_used_token(client):
    """Повтор и ротация того же токена параллельно: повтор либо отдаёт токен до ротации, либо 409."""
    for _ in range(5):
        key = str(uuid.uuid4())
        first = (await guest(client, key)).json()
        replay, rotated = await asyncio.gather(
            guest(client, key),
            client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]}),
        )
        assert rotated.status_code == 200
        assert replay.status_code in (201, 409)
        if replay.status_code == 201:
            # Повтор выдан до ротации: тот же токен в grace-окне даёт ту же новую пару.
            again = await client.post(
                "/v1/auth/refresh", json={"refreshToken": replay.json()["refreshToken"]}
            )
            assert again.json()["refreshToken"] == rotated.json()["refreshToken"]
