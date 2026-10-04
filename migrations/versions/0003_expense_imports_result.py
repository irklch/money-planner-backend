"""expense_imports: отпечаток тела запроса и сохранённый результат импорта

Повтор POST /expenses/import с тем же Idempotency-Key:
- то же тело (тот же request_hash) → сохранённый результат, расходы не создаются;
- другое тело → 409 idempotency_key_reused.
Результат хранится отдельно от expenses, поэтому последующие правки и удаления расходов
не меняют ответ повтора. Исходная выписка и её строки здесь не хранятся.

Revision ID: 0003_expense_imports_result
Revises: 0002_system_categories
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_expense_imports_result"
down_revision = "0002_system_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Колонки NOT NULL без default: на момент миграции записей в expense_imports нет
    # (backend ещё не развёрнут). Если записи появятся, миграция упадёт, а не придумает данные.
    op.add_column("expense_imports", sa.Column("request_hash", sa.LargeBinary(32), nullable=False))
    op.add_column("expense_imports", sa.Column("imported_count", sa.Integer(), nullable=False))
    op.add_column("expense_imports", sa.Column("date_from", sa.Date(), nullable=False))
    op.add_column("expense_imports", sa.Column("date_to", sa.Date(), nullable=False))
    op.create_check_constraint(
        "ck_expense_imports_result",
        "expense_imports",
        "imported_count > 0 AND date_from <= date_to AND octet_length(request_hash) = 32",
    )


def downgrade() -> None:
    op.drop_constraint("ck_expense_imports_result", "expense_imports", type_="check")
    for col in ("date_to", "date_from", "imported_count", "request_hash"):
        op.drop_column("expense_imports", col)
