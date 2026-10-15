"""Токены сессий (план §2.2, §2.8; контракт: securitySchemes, SessionTokens).

- Access JWT (HS256, 15 мин) не хранится. Claims: `typ=access`, `kind` (guest | account), `did` — id
  устройства, у аккаунта ещё `sub` = accountId (E5), `iat`, `exp`, `jti`.
- Refresh-токен — непрозрачная строка; в БД только SHA-256 (hex). Повторы (POST /auth/guest с тем же
  Idempotency-Key, refresh в grace-окне) должны вернуть тот же токен, поэтому токены не случайные, а
  детерминированно выводятся HMAC-ом с серверным секретом (`TOKEN_DERIVATION_SECRET`) из ключа или из
  предыдущего токена: открытый токен восстанавливается вычислением, а не чтением из БД.
"""

import base64
import hashlib
import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.errors import ApiError

REFRESH_PREFIX = "rt_"


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def utcnow() -> datetime:
    """Единые часы сервиса для сроков токенов и активности устройств."""
    return _clock()


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """Тесты подменяют часы, чтобы проверять сроки (grace, 24 ч, 15 мин) без ожидания."""
    global _clock
    _clock = clock or _system_clock


@dataclass(frozen=True)
class AccessClaims:
    kind: str
    device_id: uuid.UUID


def create_access_token(
    settings: Settings, kind: str, device_id: uuid.UUID, issued_at: datetime, jti: str
) -> tuple[str, datetime]:
    """Access JWT. Одинаковые аргументы дают одинаковый токен (HS256 детерминирован): повтор refresh
    в grace-окне возвращает ту же пару, что и потерянный ответ."""
    iat = int(issued_at.timestamp())
    exp = iat + settings.access_token_ttl_seconds
    payload = {"typ": "access", "kind": kind, "did": str(device_id), "iat": iat, "exp": exp, "jti": jti}
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, datetime.fromtimestamp(exp, UTC)


def decode_access_token(settings: Settings, token: str, now: datetime) -> AccessClaims:
    """Подпись и формат → `access_token_invalid`; истёк → `token_expired` (план §2.8).

    Срок проверяется по часам сервиса, а не внутри PyJWT, чтобы подпись проверялась раньше срока:
    поддельный истёкший токен — `access_token_invalid`, а не `token_expired`.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={
                "require": ["typ", "kind", "did", "iat", "exp"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as e:
        raise ApiError("access_token_invalid") from e
    # E2: только гостевые токены; токены аккаунта (`kind=account`, `sub`) добавляет E5.
    if payload["typ"] != "access" or payload["kind"] != "guest" or not isinstance(payload["exp"], int):
        raise ApiError("access_token_invalid")
    try:
        device_id = uuid.UUID(payload["did"])
    except (ValueError, TypeError, AttributeError) as e:
        raise ApiError("access_token_invalid") from e
    if payload["exp"] <= int(now.timestamp()):
        raise ApiError("token_expired")
    return AccessClaims(kind=payload["kind"], device_id=device_id)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derive(settings: Settings, purpose: str, value: str) -> bytes:
    key = settings.token_derivation_secret.get_secret_value().encode()
    return hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).digest()


def derive_refresh_token(settings: Settings, purpose: str, value: str) -> str:
    """Детерминированный refresh-токен: в БД только его хеш, повтор вычисляет его заново."""
    return REFRESH_PREFIX + base64.urlsafe_b64encode(_derive(settings, purpose, value)).rstrip(b"=").decode()


def derive_jti(settings: Settings, token_hash: str) -> str:
    """jti access-токена, выданного вместе с refresh-токеном: тот же при повторе в grace-окне."""
    return _derive(settings, "jti", token_hash)[:16].hex()


def refresh_expires_at(settings: Settings, issued_at: datetime) -> datetime:
    return issued_at + timedelta(days=settings.refresh_token_ttl_days)
