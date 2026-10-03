import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.errors import ApiError


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_access_token(settings: Settings, user_id: uuid.UUID) -> tuple[str, datetime]:
    now = utcnow()
    expires = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": "access",
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, expires


def decode_access_token(settings: Settings, token: str) -> uuid.UUID:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat", "typ"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise ApiError("access_token_expired") from e
    except jwt.PyJWTError as e:
        raise ApiError("access_token_invalid") from e
    if payload.get("typ") != "access":
        raise ApiError("access_token_invalid")
    try:
        return uuid.UUID(payload["sub"])
    except (ValueError, TypeError) as e:
        raise ApiError("access_token_invalid") from e


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derive(settings: Settings, purpose: str, value: str) -> bytes:
    key = settings.token_derivation_secret.get_secret_value().encode()
    return hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).digest()


def derive_refresh_token(settings: Settings, purpose: str, value: str) -> str:
    """Детерминированный refresh-токен.

    Нужен, чтобы повтор в grace-окне и повтор POST /auth/anonymous с тем же ключом возвращали
    тот же refresh-токен, не храня его в открытом виде (в БД только хеш).
    """
    return base64.urlsafe_b64encode(_derive(settings, purpose, value)).rstrip(b"=").decode()


def derive_uuid(settings: Settings, purpose: str, value: str) -> uuid.UUID:
    return uuid.UUID(bytes=_derive(settings, purpose, value)[:16], version=4)
