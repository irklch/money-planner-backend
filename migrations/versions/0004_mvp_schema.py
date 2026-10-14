"""Схема MVP на PostgreSQL (E1, план ред. 6, D24; модели — app/db/schema.py).

Подход (план §11.1b, docs/api-contract.md): миграции 0001–0003 не переписываются, переход только вперёд.
1. Таблицы старой схемы `users` и `refresh_tokens` переименовываются в `legacy_users` и
   `legacy_refresh_tokens` вместе с индексами и ограничениями: их имена занимает схема MVP, а старые
   `/v1/auth/*`, `DELETE /v1/me` и `/v1/imports/parse` работают на них до замены в E2–E7. Внешние ключи
   старых таблиц (`categories`, `expenses`, `free_days`, `expense_imports`) ссылаются на таблицу, а не
   на имя, и продолжают работать. Данные старой схемы сохраняются; удаление — E11.
2. Создаются 15 таблиц схемы MVP — пустые; их заполняют этапы E2–E8.

Чистая установка проходит 0001 → 0004; существующий тестовый стенд — `alembic upgrade head` с 0003 без
сброса БД. Downgrade возвращает 0003: удаляет таблицы MVP (с данными!) и возвращает прежние имена.

Revision ID: 0004_mvp_schema
Revises: 0003_expense_imports_result
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_mvp_schema"
down_revision = "0003_expense_imports_result"
branch_labels = None
depends_on = None

# (старое имя, новое имя): таблицы, а затем индексы и ограничения-индексы (их имена уникальны в схеме).
LEGACY_TABLES = [("users", "legacy_users"), ("refresh_tokens", "legacy_refresh_tokens")]
LEGACY_INDEXES = [
    ("users_pkey", "legacy_users_pkey"),
    ("refresh_tokens_pkey", "legacy_refresh_tokens_pkey"),
    ("refresh_tokens_token_hash_key", "legacy_refresh_tokens_token_hash_key"),
    ("ix_refresh_tokens_user_id", "ix_legacy_refresh_tokens_user_id"),
    ("ix_refresh_tokens_family_id", "ix_legacy_refresh_tokens_family_id"),
]

MVP_TABLES = [
    "deleted_accounts",
    "deletion_jobs",
    "rate_counters",
    "usage_counters",
    "import_ledger",
    "import_deliveries",
    "sync_mutations",
    "sync_state",
    "sync_records",
    "apple_notifications",
    "apple_tokens",
    "refresh_tokens",
    "identities",
    "devices",
    "users",
]


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def _rename(pairs: list[tuple[str, str]]) -> None:
    # Имена внешних ключей уникальны только в пределах таблицы и не конфликтуют — их не трогаем.
    for old, new in pairs:
        if (old, new) in LEGACY_TABLES or (new, old) in LEGACY_TABLES:
            op.rename_table(old, new)
        else:
            op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def upgrade() -> None:
    _rename(LEGACY_TABLES + LEGACY_INDEXES)

    op.create_table(
        "users",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'account'"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint("kind = 'account'", name="ck_users_kind"),
        sa.CheckConstraint("state IN ('active', 'deleting')", name="ck_users_state"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("app_version", sa.Text(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("last_seen_at", default_now=True),
        sa.CheckConstraint("platform IN ('ios', 'android')", name="ck_devices_platform"),
        sa.CheckConstraint("char_length(app_version) BETWEEN 1 AND 32", name="ck_devices_app_version"),
        sa.ForeignKeyConstraint(["account_id"], ["users.user_id"], name="fk_devices_account_id"),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_index("ix_devices_account_id", "devices", ["account_id"])
    op.create_index("ix_devices_last_seen_at", "devices", ["last_seen_at"])

    op.create_table(
        "identities",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        _ts("created_at", default_now=True),
        sa.CheckConstraint("provider IN ('apple')", name="ck_identities_provider"),
        sa.CheckConstraint("char_length(subject) BETWEEN 1 AND 255", name="ck_identities_subject"),
        sa.PrimaryKeyConstraint("provider", "subject", name="pk_identities"),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("expires_at"),
        _ts("rotated_at", nullable=True),
        _ts("revoked_at", nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_refresh_tokens_token_hash"),
        sa.CheckConstraint("subject_kind IN ('guest', 'account')", name="ck_refresh_tokens_subject_kind"),
        sa.CheckConstraint(
            "(subject_kind = 'account') = (user_id IS NOT NULL)", name="ck_refresh_tokens_account_user"
        ),
        sa.CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN "
            "('session_revoked', 'account_deleted', 'reuse_detected', 'logout')",
            name="ck_refresh_tokens_revoked_reason",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)", name="ck_refresh_tokens_revoked_pair"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_refresh_tokens_expiry"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], name="fk_refresh_tokens_device_id"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    for col in ("family_id", "device_id", "user_id", "expires_at"):
        op.create_index(f"ix_refresh_tokens_{col}", "refresh_tokens", [col])

    op.create_table(
        "apple_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint("key_version > 0", name="ck_apple_tokens_key_version"),
        sa.CheckConstraint("octet_length(ciphertext) > 16", name="ck_apple_tokens_ciphertext"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_apple_tokens_nonce"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_apple_tokens_user_id"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "apple_notifications",
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        _ts("received_at", default_now=True),
        _ts("processed_at", nullable=True),
        sa.PrimaryKeyConstraint("event_key"),
    )
    op.create_index("ix_apple_notifications_received_at", "apple_notifications", ["received_at"])

    op.create_table(
        "sync_records",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True),
        _ts("order_ts"),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("mutation_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("server_updated_at", default_now=True),
        sa.CheckConstraint(
            "entity_type IN ('expense', 'category', 'day_mark')", name="ck_sync_records_entity_type"
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_sync_records_schema_version"),
        sa.CheckConstraint("version > 0", name="ck_sync_records_version"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_sync_records_user_id"),
        sa.PrimaryKeyConstraint("user_id", "entity_type", "entity_id", name="pk_sync_records"),
    )
    op.create_index("uq_sync_records_user_version", "sync_records", ["user_id", "version"], unique=True)
    op.create_index(
        "ix_sync_records_tombstones",
        "sync_records",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    op.create_table(
        "sync_state",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("last_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tombstone_horizon", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint("last_version >= 0", name="ck_sync_state_last_version"),
        sa.CheckConstraint(
            "tombstone_horizon >= 0 AND tombstone_horizon <= last_version", name="ck_sync_state_horizon"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_sync_state_user_id"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "sync_mutations",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("mutation_id", sa.UUID(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ts("created_at", default_now=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_sync_mutations_user_id"),
        sa.PrimaryKeyConstraint("user_id", "mutation_id", name="pk_sync_mutations"),
    )
    op.create_index("ix_sync_mutations_created_at", "sync_mutations", ["created_at"])

    op.create_table(
        "import_deliveries",
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("idem_key", sa.UUID(), nullable=False),
        sa.Column("import_id", sa.UUID(), nullable=False),
        sa.Column("file_mac", sa.Text(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("confirmed_at", nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], name="fk_import_deliveries_device_id"),
        sa.PrimaryKeyConstraint("device_id", "idem_key", name="pk_import_deliveries"),
    )
    op.create_index("uq_import_deliveries_import_id", "import_deliveries", ["import_id"], unique=True)
    op.create_index("ix_import_deliveries_created_at", "import_deliveries", ["created_at"])

    op.create_table(
        "import_ledger",
        sa.Column("import_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("attributed_account_id", sa.UUID(), nullable=True),
        _ts("confirmed_at"),
        sa.PrimaryKeyConstraint("import_id"),
    )
    for col in ("device_id", "account_id", "attributed_account_id"):
        op.create_index(f"ix_import_ledger_{col}", "import_ledger", [col])

    op.create_table(
        "usage_counters",
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("used", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint("feature IN ('imports')", name="ck_usage_counters_feature"),
        sa.CheckConstraint(
            "subject ~ '^(device|account):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="ck_usage_counters_subject",
        ),
        sa.CheckConstraint("used >= 0", name="ck_usage_counters_used"),
        sa.PrimaryKeyConstraint("subject", "feature", name="pk_usage_counters"),
    )
    op.create_table(
        "rate_counters",
        sa.Column("counter_key", sa.Text(), nullable=False),
        _ts("window_start"),
        sa.Column("hits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("expires_at"),
        sa.CheckConstraint("expires_at > window_start", name="ck_rate_counters_expiry"),
        sa.CheckConstraint("hits >= 0", name="ck_rate_counters_hits"),
        sa.PrimaryKeyConstraint("counter_key", "window_start", name="pk_rate_counters"),
    )
    op.create_index("ix_rate_counters_expires_at", "rate_counters", ["expires_at"])

    op.create_table(
        "deletion_jobs",
        sa.Column("deletion_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_did", sa.UUID(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("lease_until", nullable=True),
        _ts("next_run_at", default_now=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("apple_revocation", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint(
            "apple_revocation IN ('pending', 'done', 'skipped_no_token', 'not_required')",
            name="ck_deletion_jobs_apple_revocation",
        ),
        sa.CheckConstraint("source IN ('user', 'apple_notification')", name="ck_deletion_jobs_source"),
        sa.CheckConstraint(
            "state IN ('pending', 'revoking', 'deleting', 'done', 'failed_retrying')",
            name="ck_deletion_jobs_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_deletion_jobs_attempts"),
        sa.PrimaryKeyConstraint("deletion_id"),
    )
    op.create_index("ix_deletion_jobs_user_id", "deletion_jobs", ["user_id"])
    op.create_index(
        "ix_deletion_jobs_next_run_at",
        "deletion_jobs",
        ["next_run_at"],
        postgresql_where=sa.text("state <> 'done'"),
    )
    op.create_index(
        "uq_deletion_jobs_active_user",
        "deletion_jobs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'done'"),
    )

    op.create_table(
        "deleted_accounts",
        sa.Column("user_id", sa.UUID(), nullable=False),
        _ts("deleted_at", default_now=True),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("ix_deleted_accounts_deleted_at", "deleted_accounts", ["deleted_at"])


def downgrade() -> None:
    for table in MVP_TABLES:  # индексы удаляются вместе с таблицами; порядок — от зависимых к главным
        op.drop_table(table)
    _rename([(new, old) for old, new in LEGACY_INDEXES + LEGACY_TABLES])
