import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.ai.client import LLMError
from app.ai.provider import get_llm_client
from app.core.deps import CurrentUserDep, SessionDep, SettingsDep
from app.core.errors import ApiError
from app.core.rate_limit import limiter
from app.core.schemas import ApiModel
from app.modules.analytics.router import parse_compare, parse_range
from app.modules.insights import service

router = APIRouter(prefix="/insights", tags=["insights"])


class InsightTarget(ApiModel):
    type: Literal["category", "period"]
    category_id: uuid.UUID | None = None


class InsightOut(ApiModel):
    id: str
    title: str
    detail: str
    target: InsightTarget


class InsightsOut(ApiModel):
    items: list[InsightOut]


@router.get("", response_model=InsightsOut, response_model_exclude_none=True)
async def get_insights(
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    from_: str = Query(alias="from"),
    to: str = Query(),
    compareFrom: str | None = None,  # noqa: N803
    compareTo: str | None = None,  # noqa: N803
) -> InsightsOut:
    start, end = parse_range(from_, to)
    compare = parse_compare(compareFrom, compareTo)
    limiter.hit(f"insights:{user.id}", settings.rate_insights_per_minute)
    client = get_llm_client()
    if client is None:
        raise ApiError("insights_unavailable")
    try:
        items = await service.generate(session, client, user.id, start, end, compare)
    except LLMError:
        raise ApiError("insights_unavailable") from None
    return InsightsOut(
        items=[
            InsightOut(
                id=i.id,
                title=i.title,
                detail=i.detail,
                target=InsightTarget(type=i.target_type, category_id=i.category_id),
            )
            for i in items
        ]
    )
