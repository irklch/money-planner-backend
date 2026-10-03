"""Жизненный цикл гостевой сессии (02 — Authentication).

- Access JWT (15 мин) не хранится. Refresh-токен — opaque, в БД только SHA-256.
- Ротация: каждый refresh помечает старый токен rotated_at и выдаёт новый в той же family.
- Grace-окно: повтор старого токена в окне возвращает тот же новый refresh-токен
  (он детерминированно выводится из старого через HMAC с серверным секретом).
- Повтор старого токена вне окна = reuse → отзыв всей family.
- UUID пользователя сам по себе доступа не даёт: доступ только через refresh-токен.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import (
    create_access_token,
    derive_refresh_token,
    derive_uuid,
    hash_refresh_token,
    utcnow,
)
from app.db.models import RefreshToken, User
from app.modules.auth.schemas import AuthSession

ROTATE = "rotate"
ANON = "anon"


def _lock_key(value: str) -> int:
    return int.from_bytes(uuid.uuid5(uuid.NAMESPACE_OID, value).bytes[:8], "big", signed=True)


def _session(settings: Settings, user_id: uuid.UUID, refresh_token: str, row: RefreshToken) -> AuthSession:
    access, access_exp = create_access_token(settings, user_id)
    return AuthSession(
        user_id=user_id,
        is_anonymous=True,
        access_token=access,
        access_token_expires_at=access_exp,
        refresh_token=refresh_token,
        refresh_token_expires_at=row.expires_at,
    )


def _new_row(settings: Settings, user_id: uuid.UUID, token: str, family_id: uuid.UUID) -> RefreshToken:
    return RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=hash_refresh_token(token),
        family_id=family_id,
        expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
    )


async def create_anonymous(
    session: AsyncSession, settings: Settings, key: uuid.UUID
) -> tuple[AuthSession, bool]:
    """Возвращает (сессия, replayed). Тот же Idempotency-Key → тот же guest и тот же refresh-токен."""
    token = derive_refresh_token(settings, ANON, str(key))
    token_hash = hash_refresh_token(token)
    await session.execute(select(func.pg_advisory_xact_lock(_lock_key(f"anon:{key}"))))

    existing = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if existing is not None:
        window = timedelta(hours=settings.anonymous_idempotency_window_hours)
        if existing.rotated_at or existing.revoked_at or existing.created_at < utcnow() - window:
            raise ApiError("idempotency_key_reused")
        await session.commit()
        return _session(settings, existing.user_id, token, existing), True

    user = User(id=uuid.uuid4(), is_anonymous=True)
    session.add(user)
    await session.flush()
    row = _new_row(settings, user.id, token, derive_uuid(settings, "anon-family", str(key)))
    session.add(row)
    await session.commit()
    return _session(settings, user.id, token, row), False


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await session.commit()


async def refresh(session: AsyncSession, settings: Settings, token: str) -> AuthSession:
    now = utcnow()
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(token)).with_for_update()
    )
    if row is None or row.revoked_at is not None:
        await session.rollback()
        raise ApiError("refresh_token_invalid")
    if row.expires_at <= now:
        await session.rollback()
        raise ApiError("refresh_token_expired")

    child_token = derive_refresh_token(settings, ROTATE, token)

    if row.rotated_at is not None:
        if now - row.rotated_at <= timedelta(seconds=settings.refresh_grace_seconds):
            child = await session.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(child_token))
            )
            if child is not None and child.revoked_at is None and child.rotated_at is None:
                await session.commit()
                return _session(settings, row.user_id, child_token, child)
        # Повторное использование старого токена: отзываем всю цепочку.
        await _revoke_family(session, row.family_id)
        raise ApiError("refresh_token_invalid")

    row.rotated_at = now
    child = _new_row(settings, row.user_id, child_token, row.family_id)
    session.add(child)
    await session.execute(update(User).where(User.id == row.user_id).values(last_seen_at=now))
    await session.commit()
    return _session(settings, row.user_id, child_token, child)
