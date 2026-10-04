"""Повтор POST /auth/anonymous с тем же Idempotency-Key (п. 8)."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.core.security import derive_refresh_token, hash_refresh_token, utcnow
from app.db.models import RefreshToken, User
from app.db.session import sessionmaker

BODY = {"platform": "ios", "appVersion": "1.0"}


async def anon(client, key):
    return await client.post("/v1/auth/anonymous", json=BODY, headers={"Idempotency-Key": key})


async def counts():
    async with sessionmaker()() as s:
        return (
            await s.scalar(select(func.count()).select_from(User)),
            await s.scalar(select(func.count()).select_from(RefreshToken)),
        )


async def test_replay_recomputes_same_token_and_db_has_only_hash(client):
    key = str(uuid.uuid4())
    first = (await anon(client, key)).json()
    replay = await anon(client, key)
    assert replay.status_code == 201 and replay.headers["Idempotent-Replayed"] == "true"
    second = replay.json()

    # Тот же guest, тот же refresh-токен и срок; access-токен — новый JWT того же userId.
    for f in ("userId", "isAnonymous", "refreshToken", "refreshTokenExpiresAt"):
        assert first[f] == second[f]
    assert await counts() == (1, 1)  # второй guest и вторая запись токена не созданы

    # Токен восстанавливается вычислением, а не чтением из БД:
    token = derive_refresh_token(get_settings(), "anon", key)
    assert token == first["refreshToken"]
    async with sessionmaker()() as s:
        row = (await s.execute(text("SELECT * FROM refresh_tokens"))).mappings().one()
        assert row["token_hash"] == hash_refresh_token(token)
        # Открытого токена и самого ключа нет ни в одной колонке.
        dump = " ".join(str(v) for v in row.values())
        assert first["refreshToken"] not in dump and key not in dump


async def test_replay_after_rotation_returns_no_token(client):
    key = str(uuid.uuid4())
    first = (await anon(client, key)).json()
    rotated = await client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]})
    assert rotated.status_code == 200

    r = await anon(client, key)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert first["refreshToken"] not in r.text and rotated.json()["refreshToken"] not in r.text
    assert (await counts())[0] == 1  # нового guest нет
    # Актуальная сессия не задета.
    ok = await client.post("/v1/auth/refresh", json={"refreshToken": rotated.json()["refreshToken"]})
    assert ok.status_code == 200


async def test_replay_after_revocation_or_window_returns_no_token(client):
    key = str(uuid.uuid4())
    first = (await anon(client, key)).json()
    token_hash = hash_refresh_token(first["refreshToken"])
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken).where(RefreshToken.token_hash == token_hash).values(revoked_at=utcnow())
        )
        await s.commit()
    r = await anon(client, key)
    assert r.status_code == 409 and first["refreshToken"] not in r.text

    key2 = str(uuid.uuid4())
    first2 = (await anon(client, key2)).json()
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(first2["refreshToken"]))
            .values(created_at=utcnow() - timedelta(hours=25))
        )
        await s.commit()
    r = await anon(client, key2)
    assert r.status_code == 409 and first2["refreshToken"] not in r.text


async def test_parallel_first_requests_create_one_guest(client):
    import asyncio

    key = str(uuid.uuid4())
    rs = await asyncio.gather(*[anon(client, key) for _ in range(5)])
    assert all(r.status_code == 201 for r in rs)
    assert len({r.json()["userId"] for r in rs}) == 1
    assert await counts() == (1, 1)
