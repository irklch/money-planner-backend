import uuid
from datetime import timedelta

from sqlalchemy import select, update

from app.core.security import hash_refresh_token, utcnow
from app.db.models import RefreshToken
from app.db.session import sessionmaker
from tests.conftest import auth, new_guest


async def test_anonymous_idempotent_same_guest(client):
    key = str(uuid.uuid4())
    body = {"platform": "ios", "appVersion": "1.0"}
    r1 = await client.post("/v1/auth/anonymous", json=body, headers={"Idempotency-Key": key})
    r2 = await client.post("/v1/auth/anonymous", json=body, headers={"Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotent-Replayed"] == "true"
    a, b = r1.json(), r2.json()
    assert a["userId"] == b["userId"]
    assert a["refreshToken"] == b["refreshToken"]
    assert a["isAnonymous"] is True


async def test_anonymous_requires_idempotency_key(client):
    r = await client.post("/v1/auth/anonymous", json={"platform": "ios", "appVersion": "1"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_access_token_required_and_invalid(client):
    r = await client.get("/v1/categories")
    assert r.status_code == 401 and r.json()["error"]["code"] == "access_token_invalid"
    r = await client.get("/v1/categories", headers={"Authorization": "Bearer garbage"})
    assert r.json()["error"]["code"] == "access_token_invalid"
    assert r.headers["X-Request-Id"] == r.json()["error"]["requestId"]


async def test_user_id_alone_gives_no_access(client, guest):
    # Подделать JWT с чужим sub без секрета нельзя.
    import jwt

    forged = jwt.encode(
        {"sub": guest["userId"], "typ": "access", "iat": 0, "exp": 9999999999}, "wrong", "HS256"
    )
    r = await client.get("/v1/categories", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


async def test_refresh_rotation_grace_and_reuse(client, guest):
    old = guest["refreshToken"]
    r1 = await client.post("/v1/auth/refresh", json={"refreshToken": old})
    assert r1.status_code == 200
    new = r1.json()["refreshToken"]
    assert new != old and r1.json()["userId"] == guest["userId"]

    # Повтор старого токена в grace-окне → тот же новый refresh-токен.
    r2 = await client.post("/v1/auth/refresh", json={"refreshToken": old})
    assert r2.status_code == 200 and r2.json()["refreshToken"] == new

    # Вне grace-окна повтор старого токена = reuse → отзыв всей цепочки.
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(old))
            .values(rotated_at=utcnow() - timedelta(minutes=5))
        )
        await s.commit()
    r3 = await client.post("/v1/auth/refresh", json={"refreshToken": old})
    assert r3.status_code == 401 and r3.json()["error"]["code"] == "refresh_token_invalid"
    r4 = await client.post("/v1/auth/refresh", json={"refreshToken": new})
    assert r4.status_code == 401 and r4.json()["error"]["code"] == "refresh_token_invalid"


async def test_refresh_expired(client, guest):
    async with sessionmaker()() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(guest["refreshToken"]))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await s.commit()
    r = await client.post("/v1/auth/refresh", json={"refreshToken": guest["refreshToken"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_expired"


async def test_refresh_unknown_token(client):
    r = await client.post("/v1/auth/refresh", json={"refreshToken": "x" * 40})
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_invalid"


async def test_refresh_token_stored_only_as_hash(client, guest):
    async with sessionmaker()() as s:
        hashes = (await s.scalars(select(RefreshToken.token_hash))).all()
    assert guest["refreshToken"] not in hashes
    assert hash_refresh_token(guest["refreshToken"]) in hashes


async def test_delete_me_removes_everything_and_is_idempotent(client, guest):
    h = auth(guest)
    r = await client.delete("/v1/me", headers=h)
    assert r.status_code == 204
    r = await client.delete("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "user_deleted"
    r = await client.post("/v1/auth/refresh", json={"refreshToken": guest["refreshToken"]})
    assert r.status_code == 401
    other = await new_guest(client)
    r = await client.get("/v1/categories", headers=auth(other))
    assert any(c["name"] == "Другое" for c in r.json()["items"])  # системные категории на месте
