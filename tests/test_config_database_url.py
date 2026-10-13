import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    # Тестовое окружение задаёт DATABASE_URL; здесь проверяется только сборка из аргументов.
    for var in ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


# Пароль со всеми символами, которые ломают URL при простой подстановке.
TRICKY = "a/b+c=d@e:f%g?h&i[j]k~l.m_n-o"


def parts(password=TRICKY, **kw):
    return {"db_host": "db", "db_name": "money", "db_user": "money", "db_password": password, **kw}


def test_url_from_parts_keeps_special_characters():
    url = make_url(Settings(**parts()).database_url.get_secret_value())
    assert url.drivername == "postgresql+asyncpg"
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "money",
        TRICKY,
        "db",
        5432,
        "money",
    )


def test_base64_password_survives():
    # openssl rand -base64 48: символы + / =
    pw = "q1/2+3==Zx/Yw+Vu/Ts9Rq8Po7Nm6Lk5Jh4Gf3Ed2Cb1Aa0Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp="
    assert make_url(Settings(**parts(pw)).database_url.get_secret_value()).password == pw


def test_parts_and_url_together_rejected():
    with pytest.raises(ValidationError, match="not both"):
        Settings(database_url="postgresql+asyncpg://x:y@h/db", **parts())


def test_incomplete_parts_rejected():
    with pytest.raises(ValidationError, match="DB_USER"):
        Settings(db_host="db", db_name="money", db_password="x")


def test_plain_database_url_unchanged():
    url = "postgresql+asyncpg://money:money@localhost:5432/money_test"
    assert Settings(database_url=url).database_url.get_secret_value() == url
