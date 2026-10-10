"""Идентификация в прототипе.

`userId` берётся ТОЛЬКО из подписанного токена (HS256, `sub`). В теле и query его нет, лишние
поля запрещены схемой. Токен может выпустить только тот, у кого есть SYNC_JWT_SECRET:
- локально и в тестах секрет генерируется случайно на запуск и не попадает в репозиторий;
- в облачном эксперименте секрет лежит в Lockbox, токены выпускает оператор benchmark.
Публичного endpoint для выпуска токенов нет. В production этот слой заменяется своими JWT,
выданными после Sign in with Apple (проверка в зависимости `current_user` та же).
"""

from __future__ import annotations

# time — время выпуска и срок токена; uuid — проверка формата userId; jwt — PyJWT.
import time
import uuid

import jwt

# Издатель токенов прототипа.
ISSUER = "money-planner-sync-proto"


# Токен не прошёл проверку → 401.
class AuthError(Exception):
    pass


# Выпустить тестовый токен для userId (используют тесты, benchmark и облачные замеры).
def issue_test_token(secret: str, user_id: str, audience: str, ttl_s: int = 3600) -> str:
    uuid.UUID(user_id)  # только UUID
    now = int(time.time())
    claims = {"sub": user_id, "iss": ISSUER, "aud": audience, "iat": now, "exp": now + ttl_s}
    return jwt.encode(claims, secret, algorithm="HS256")


# Проверить токен и вернуть userId. Принимается только HS256 с нашим секретом, издателем и аудиторией.
def verify(token: str, secret: str, audience: str) -> str:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=audience,
            issuer=ISSUER,
            options={"require": ["sub", "exp", "iat", "aud", "iss"]},
        )
        return str(uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError) as e:
        raise AuthError(str(e)) from e
