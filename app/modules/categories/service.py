import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import utcnow
from app.db.models import Category
from app.modules.categories.schemas import CategoryCreate, CategoryOut, CategoryUpdate

OTHER_SORT_ORDER = 99  # «Другое» — последняя


def _lock_key(user_id: uuid.UUID) -> int:
    return int.from_bytes(user_id.bytes[:8], "big", signed=True) ^ 0x0C47


def to_out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=c.id,
        name=c.name,
        emoji=c.emoji,
        kind="system" if c.is_system else "custom",
        color_index=c.color_index,
        sort_order=c.sort_order,
        archived_at=c.archived_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _visible(user_id: uuid.UUID):
    return or_(Category.user_id.is_(None), Category.user_id == user_id)


def _order_key(c: Category):
    if c.is_system:
        group = 2 if (c.sort_order or 0) >= OTHER_SORT_ORDER else 0
        return (group, c.sort_order or 0, c.created_at)
    return (1, 0, c.created_at)


async def list_categories(
    session: AsyncSession, user_id: uuid.UUID, include_archived: bool
) -> list[Category]:
    q = select(Category).where(_visible(user_id))
    if not include_archived:
        q = q.where(Category.archived_at.is_(None))
    rows = list((await session.scalars(q)).all())
    rows.sort(key=_order_key)
    return rows


async def active_categories(session: AsyncSession, user_id: uuid.UUID) -> list[Category]:
    return await list_categories(session, user_id, include_archived=False)


async def _ensure_name_free(
    session: AsyncSession, user_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
) -> None:
    names = await session.execute(
        select(Category.id, Category.name).where(_visible(user_id), Category.archived_at.is_(None))
    )
    folded = name.casefold()
    for cid, existing in names:
        if cid != exclude_id and existing.strip().casefold() == folded:
            raise ApiError("category_name_taken")


async def create_category(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, key: uuid.UUID, body: CategoryCreate
) -> tuple[Category, bool]:
    """Idempotency-Key = id новой категории: повтор с тем же ключом возвращает ту же категорию."""
    await session.execute(select(func.pg_advisory_xact_lock(_lock_key(user_id))))
    existing = await session.get(Category, key)
    if existing is not None:
        if existing.user_id != user_id:
            raise ApiError("idempotency_key_reused")
        await session.commit()
        return existing, True

    await _ensure_name_free(session, user_id, body.name)
    used = await session.scalar(select(func.count()).select_from(Category).where(_visible(user_id)))
    category = Category(
        id=key,
        user_id=user_id,
        name=body.name,
        emoji=body.emoji,
        color_index=int(used or 0) % settings.category_palette_size,
        sort_order=None,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category, False


async def _get_owned_for_write(session: AsyncSession, user_id: uuid.UUID, category_id: uuid.UUID) -> Category:
    c = await session.scalar(
        select(Category).where(Category.id == category_id, _visible(user_id)).with_for_update()
    )
    if c is None:
        raise ApiError("category_not_found")
    if c.is_system:
        raise ApiError("category_readonly")
    return c


async def update_category(
    session: AsyncSession, user_id: uuid.UUID, category_id: uuid.UUID, body: CategoryUpdate
) -> Category:
    if body.name is None and body.emoji is None:
        raise ApiError("invalid_request")
    await session.execute(select(func.pg_advisory_xact_lock(_lock_key(user_id))))
    c = await _get_owned_for_write(session, user_id, category_id)
    if c.archived_at is not None:
        raise ApiError("category_archived")
    if body.name is not None and body.name != c.name:
        await _ensure_name_free(session, user_id, body.name, exclude_id=c.id)
        c.name = body.name
    if body.emoji is not None:
        c.emoji = body.emoji
    c.updated_at = utcnow()
    await session.commit()
    await session.refresh(c)
    return c


async def archive_category(session: AsyncSession, user_id: uuid.UUID, category_id: uuid.UUID) -> Category:
    c = await _get_owned_for_write(session, user_id, category_id)
    if c.archived_at is None:
        now = utcnow()
        c.archived_at = now
        c.updated_at = now
        await session.commit()
        await session.refresh(c)
    else:
        await session.commit()
    return c


class CategoryResolver:
    """Проверка categoryId для записи расходов: своя или системная, активная."""

    def __init__(self, categories: list[Category]):
        self._by_id = {c.id: c for c in categories}

    @classmethod
    async def load(cls, session: AsyncSession, user_id: uuid.UUID, ids: set[uuid.UUID]) -> "CategoryResolver":
        if not ids:
            return cls([])
        rows = await session.scalars(select(Category).where(Category.id.in_(ids), _visible(user_id)))
        return cls(list(rows.all()))

    def check(self, category_id: uuid.UUID, allow_archived_id: uuid.UUID | None = None) -> str | None:
        """None — ок; иначе код details[]: category_not_found | category_archived."""
        c = self._by_id.get(category_id)
        if c is None:
            return "category_not_found"
        if c.archived_at is not None and category_id != allow_archived_id:
            return "category_archived"
        return None
