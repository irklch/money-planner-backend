from datetime import date

from fastapi import APIRouter, Query

from app.core.deps import ClientTodayDep, CurrentUserDep, SessionDep
from app.core.errors import ErrorDetail, validation_error
from app.core.schemas import ApiModel, parse_date_param
from app.modules.calendar import service

router = APIRouter(prefix="/calendar", tags=["calendar"])


class CalendarDay(ApiModel):
    date: date
    expense_count: int
    is_acknowledged_empty: bool
    is_accounted: bool


class CalendarDays(ApiModel):
    days: list[CalendarDay]


@router.get("/days", response_model=CalendarDays)
async def get_days(
    user: CurrentUserDep, session: SessionDep, from_: str = Query(alias="from"), to: str = Query()
) -> CalendarDays:
    start, end = parse_date_param(from_, "range_invalid"), parse_date_param(to, "range_invalid")
    if start > end:
        raise validation_error(ErrorDetail(code="range_invalid"))
    if (end - start).days + 1 > service.MAX_RANGE_DAYS:
        raise validation_error(ErrorDetail(code="range_too_large"))
    days = await service.get_days(session, user.id, start, end)
    return CalendarDays(days=[CalendarDay(**d) for d in days])


@router.put("/acknowledgements/{day}", response_model=CalendarDay)
async def acknowledge(
    day: str, user: CurrentUserDep, today: ClientTodayDep, session: SessionDep
) -> CalendarDay:
    return CalendarDay(**await service.acknowledge(session, user.id, parse_date_param(day), today))


@router.delete("/acknowledgements/{day}", response_model=CalendarDay)
async def unacknowledge(day: str, user: CurrentUserDep, session: SessionDep) -> CalendarDay:
    return CalendarDay(**await service.unacknowledge(session, user.id, parse_date_param(day)))
