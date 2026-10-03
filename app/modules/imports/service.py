"""POST /imports/parse: файл → банк → расходные операции → автокатегоризация → ParseResult.

Stateless: ни файл, ни результат разбора на сервере не сохраняются (ни в БД, ни на диске).
"""

import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMError
from app.ai.provider import get_llm_client
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.schemas import MAX_AMOUNT
from app.modules.categories.service import active_categories
from app.modules.imports.categorization import categorize
from app.modules.imports.parsing import registry
from app.modules.imports.parsing.base import StatementError, UnreadRow
from app.modules.imports.parsing.reader import detect_format, read_table
from app.modules.imports.schemas import BankOut, ParsedOperation, ParseResult, PartiallyReadWarning, PeriodOut

log = logging.getLogger("app.imports")

COMMENT_MAX = 200


async def parse_statement(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, filename: str | None, data: bytes
) -> ParseResult:
    try:
        fmt = detect_format(filename)
        if not data:
            raise StatementError("empty_statement")
        # Разбор — CPU-bound; выносим из event loop.
        table = await asyncio.to_thread(read_table, data, fmt)
        adapter = registry.detect(table)
        if adapter is None:
            raise StatementError("unknown_bank")
        result = await asyncio.to_thread(adapter.parse, table)
    except StatementError as e:
        raise ApiError(e.code) from None

    unread = list(result.unread)
    expenses, income = [], 0
    for op in result.operations:
        if op.amount < 0:
            if -op.amount > MAX_AMOUNT:
                unread.append(UnreadRow(row=op.row, date=op.date))
                continue
            expenses.append(op)
        elif op.amount > 0:
            income += 1

    if not result.operations:
        raise ApiError("corrupted_file" if unread else "empty_statement")
    if not expenses:
        raise ApiError("income_only")

    categories = await active_categories(session, user_id)
    try:
        guesses = await categorize(
            [op.description for op in expenses],
            categories,
            get_llm_client(),
            settings.ai_categorize_batch_size,
        )
    except LLMError:
        raise ApiError("processing_failed") from None

    all_dates = [op.date for op in result.operations]
    period = result.period or (min(all_dates), max(all_dates))
    warnings = []
    if unread:
        warnings.append(
            PartiallyReadWarning(
                unread_dates=sorted({u.date for u in unread if u.date is not None}),
                unread_row_count=len(unread),
            )
        )
    log.info("statement_parsed", extra={"bank": adapter.code, "count": len(expenses)})
    return ParseResult(
        bank=BankOut(code=adapter.code, name=adapter.name),
        period=PeriodOut(from_=period[0], to=period[1]),
        operations=[
            ParsedOperation(
                date=op.date,
                amount=Decimal(-op.amount),
                comment=(op.description or None) and op.description[:COMMENT_MAX],
                category_id=g.category_id,
                category_status=g.status,
            )
            for op, g in zip(expenses, guesses, strict=True)
        ],
        skipped_income_count=income,
        warnings=warnings,
    )
