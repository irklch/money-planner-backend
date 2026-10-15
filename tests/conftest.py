import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://money:money@localhost:5432/money_test")

from app.ai.provider import set_llm_client  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.core.security import set_clock  # noqa: E402
from app.db.session import sessionmaker  # noqa: E402
from app.main import app  # noqa: E402


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture(scope="session", autouse=True)
async def database():
    url = os.environ["DATABASE_URL"]
    admin = os.environ.get("ADMIN_DATABASE_URL", url.rsplit("/", 1)[0] + "/postgres")
    dbname = url.rsplit("/", 1)[1]
    conn = await asyncpg.connect(_plain(admin))
    await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    await conn.execute(f'CREATE DATABASE "{dbname}"')
    await conn.close()
    cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    await asyncio.to_thread(subprocess.run, cmd, check=True, env=os.environ.copy())
    yield


@pytest.fixture(autouse=True)
async def clean():
    limiter.reset()
    set_llm_client(None)
    yield
    set_clock(None)
    limiter.reset()
    async with sessionmaker()() as s:
        await s.execute(text("DELETE FROM legacy_users"))
        await s.execute(text("DELETE FROM refresh_tokens"))
        await s.execute(text("DELETE FROM devices"))
        await s.execute(text("DELETE FROM rate_counters"))
        await s.commit()
    set_llm_client(None)


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Timezone": "Europe/Moscow"}
    ) as c:
        yield c


async def new_guest(client: AsyncClient) -> dict:
    r = await client.post(
        "/v1/auth/guest",
        json={"platform": "ios", "appVersion": "1.0"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    return r.json()


def auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['accessToken']}"}


@pytest.fixture
async def guest(client):
    return await new_guest(client)


@pytest.fixture
async def headers(guest):
    return auth(guest)


class Clock:
    """Часы сервиса под управлением теста (app.core.security.set_clock)."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    c = Clock()
    set_clock(c)
    yield c
    set_clock(None)


PRODUCTS = "00000000-0000-4000-8000-000000000001"
CAFE = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000099"
