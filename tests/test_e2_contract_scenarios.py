"""Сценарии контракта `contract/tests/test_contract_scenarios.py::TestAuth` против приложения (E2).

Шаги и ожидания — те же, что на эталонной модели; время — часы сервиса (`clock`), ответы проверяются
тем же `conforms()`. Части сценариев, которые требуют эндпоинтов следующих этапов, не запускаются:
`/v1/me` (E4) заменён здесь защищённым маршрутом, доступным в E2; вход Apple, выход и удаление — E5, E7.
"""

import uuid

from tests.contract import conforms, rm

GRACE_S = rm.GRACE_S
ACCESS_TTL_S = rm.ACCESS_TTL_S


class App:
    """Тот же интерфейс, что у `rm.RefServer`, но запросы идут в приложение."""

    def __init__(self, client, clock):
        self.client, self.clock = client, clock

    def tick(self, seconds: float) -> None:
        self.clock.tick(seconds)

    async def guest(self, key: str):
        r = await self.client.post(
            "/v1/auth/guest",
            json={"platform": "ios", "appVersion": "1.0.0"},
            headers={"Idempotency-Key": key},
        )
        conforms("post", "/v1/auth/guest", r)
        return r

    async def refresh_session(self, token: str):
        r = await self.client.post("/v1/auth/refresh", json={"refreshToken": token})
        conforms("post", "/v1/auth/refresh", r)
        return r

    async def protected(self, access: str):
        """Вместо `/v1/me` (E4): гостевой маршрут E2; после авторизации без файла — 400."""
        r = await self.client.post("/v1/imports/parse", headers={"Authorization": f"Bearer {access}"})
        conforms("post", "/v1/imports/parse", r)
        return r


def code(r):
    return r.json()["error"]["code"]


async def new_guest(app):
    return (await app.guest(str(uuid.uuid4()))).json()


async def test_guest_replay_and_reuse(client, clock):
    app = App(client, clock)
    key = str(uuid.uuid4())
    first = await app.guest(key)
    again = await app.guest(key)
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    assert (again.json()["deviceId"], again.json()["refreshToken"]) == (
        first.json()["deviceId"],
        first.json()["refreshToken"],
    )
    await app.refresh_session(first.json()["refreshToken"])
    assert code(await app.guest(key)) == "idempotency_key_reused"
    assert code(await app.guest("not-a-uuid")) == "invalid_request"


async def test_refresh_lost_response_within_grace(client, clock):
    app = App(client, clock)
    g = await new_guest(app)
    lost = await app.refresh_session(g["refreshToken"])
    app.tick(GRACE_S - 1)
    retry = await app.refresh_session(g["refreshToken"])
    assert retry.status_code == 200 and retry.json() == lost.json()


async def test_refresh_lost_response_beyond_grace_guest(client, clock):
    app = App(client, clock)
    g = await new_guest(app)
    lost = await app.refresh_session(g["refreshToken"])
    app.tick(GRACE_S + 1)
    retry = await app.refresh_session(g["refreshToken"])
    assert retry.status_code == 401 and code(retry) == "refresh_token_invalid"
    stolen = await app.refresh_session(lost.json()["refreshToken"])
    assert code(stolen) == "refresh_token_invalid"
    g2 = await new_guest(app)
    assert g2["deviceId"] != g["deviceId"]
    # Счётчик импортов нового устройства с нуля проверяет E4 (`/v1/me`).


async def test_401_codes(client, clock):
    app = App(client, clock)
    g = await new_guest(app)
    assert code(await app.protected("garbage")) == "access_token_invalid"
    app.tick(ACCESS_TTL_S + 1)
    assert code(await app.protected(g["accessToken"])) == "token_expired"
    fresh = (await app.refresh_session(g["refreshToken"])).json()
    assert (await app.protected(fresh["accessToken"])).status_code == 400  # авторизация пройдена
