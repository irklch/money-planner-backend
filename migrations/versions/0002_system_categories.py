"""Seed system categories

ЧЕРНОВИК СПИСКА: финальный перечень системных категорий в макетах не зафиксирован
(в UX встречаются «Продукты», «Кафе и рестораны», «Транспорт», «Другое»). Список согласовать.
id фиксированы навсегда; «Другое» — последняя, не архивируется и не переименовывается.

Revision ID: 0002_system_categories
Revises: 0001_initial_schema
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_system_categories"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

SYSTEM_CATEGORIES = [
    # id, name, emoji, color_index, sort_order
    ("00000000-0000-4000-8000-000000000001", "Продукты", "🛒", 0, 1),
    ("00000000-0000-4000-8000-000000000002", "Кафе и рестораны", "🍽️", 1, 2),
    ("00000000-0000-4000-8000-000000000003", "Транспорт", "🚕", 2, 3),
    ("00000000-0000-4000-8000-000000000004", "Дом и ЖКХ", "🏠", 3, 4),
    ("00000000-0000-4000-8000-000000000005", "Здоровье", "💊", 4, 5),
    ("00000000-0000-4000-8000-000000000006", "Одежда и обувь", "👕", 5, 6),
    ("00000000-0000-4000-8000-000000000007", "Развлечения", "🎬", 6, 7),
    ("00000000-0000-4000-8000-000000000008", "Связь и подписки", "📱", 7, 8),
    ("00000000-0000-4000-8000-000000000009", "Красота", "💅", 8, 9),
    ("00000000-0000-4000-8000-00000000000a", "Подарки", "🎁", 9, 10),
    ("00000000-0000-4000-8000-00000000000b", "Путешествия", "✈️", 10, 11),
    ("00000000-0000-4000-8000-000000000099", "Другое", "📦", 11, 99),
]


def upgrade() -> None:
    conn = op.get_bind()
    for cid, name, emoji, color, order in SYSTEM_CATEGORIES:
        conn.execute(
            sa.text(
                "INSERT INTO categories (id, user_id, name, emoji, color_index, sort_order) "
                "VALUES (CAST(:id AS uuid), NULL, :name, :emoji, :color, :order) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, emoji = EXCLUDED.emoji, "
                "color_index = EXCLUDED.color_index, sort_order = EXCLUDED.sort_order, updated_at = now()"
            ),
            {"id": cid, "name": name, "emoji": emoji, "color": color, "order": order},
        )


def downgrade() -> None:
    conn = op.get_bind()
    ids = [c[0] for c in SYSTEM_CATEGORIES]
    conn.execute(
        sa.text("DELETE FROM categories WHERE user_id IS NULL AND id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": ids},
    )
