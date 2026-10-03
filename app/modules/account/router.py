from fastapi import APIRouter, Response
from sqlalchemy import delete

from app.core.deps import CurrentUserDep, SessionDep
from app.db.models import User

router = APIRouter(tags=["account"])


@router.delete("/me", status_code=204)
async def delete_me(user: CurrentUserDep, session: SessionDep) -> Response:
    """Удаляет guest и всё, что ему принадлежит (каскад по FK): expenses, свои категории
    (включая архивные), free_days, expense_imports, refresh_tokens. Системные категории не трогаются.
    Повтор после успеха → 401 user_deleted (iOS считает это успехом)."""
    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()
    return Response(status_code=204)
