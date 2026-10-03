import uuid

from fastapi import APIRouter, Response

from app.core.deps import CurrentUserDep, IdempotencyKeyDep, SessionDep, SettingsDep
from app.modules.categories import service
from app.modules.categories.schemas import CategoryCreate, CategoryList, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=CategoryList)
async def list_categories(
    user: CurrentUserDep,
    session: SessionDep,
    includeArchived: bool = False,  # noqa: N803
) -> CategoryList:
    rows = await service.list_categories(session, user.id, includeArchived)
    return CategoryList(items=[service.to_out(c) for c in rows])


@router.post("", status_code=201, response_model=CategoryOut)
async def create_category(
    body: CategoryCreate,
    response: Response,
    user: CurrentUserDep,
    key: IdempotencyKeyDep,
    session: SessionDep,
    settings: SettingsDep,
) -> CategoryOut:
    c, replayed = await service.create_category(session, settings, user.id, key, body)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return service.to_out(c)


@router.patch("/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: uuid.UUID, body: CategoryUpdate, user: CurrentUserDep, session: SessionDep
) -> CategoryOut:
    return service.to_out(await service.update_category(session, user.id, category_id, body))


@router.delete("/{category_id}", response_model=CategoryOut)
async def archive_category(category_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> CategoryOut:
    return service.to_out(await service.archive_category(session, user.id, category_id))
