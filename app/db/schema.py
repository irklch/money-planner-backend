"""Схема MVP на PostgreSQL (план ред. 6, D24; архитектура §7.1). Миграция — 0004_mvp_schema.

Таблицы создаются в E1; логику, которая их пишет и читает, добавляют этапы E2–E8 (указаны у таблиц).
Допустимые значения — CHECK-ограничения с именами из контракта (`contract/openapi.yaml`), а не ENUM-типы:
их проще расширять миграциями. Сроки хранения (в PostgreSQL нет TTL) соблюдают задачи `maintenance`
(E8), поэтому у таких таблиц есть индекс по сроку.

Внешние ключи — без каскадов и только там, где их не нарушает порядок удаления аккаунта (план §2.5):
sync_* → import_ledger и счётчики → apple_tokens → отвязка devices.account_id → users → identities.
Поэтому у identities, deletion_jobs, deleted_accounts, import_ledger и refresh_tokens.user_id ключа на
users нет: эти строки по плану переживают строку users.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


PLATFORMS = ("ios", "android")
SUBJECT_KINDS = ("guest", "account")
USER_STATES = ("active", "deleting")
IDENTITY_PROVIDERS = ("apple",)
REVOKED_REASONS = ("session_revoked", "account_deleted", "reuse_detected", "logout")
ENTITY_TYPES = ("expense", "category", "day_mark")
USAGE_FEATURES = ("imports",)
DELETION_SOURCES = ("user", "apple_notification")
DELETION_STATES = ("pending", "revoking", "deleting", "done", "failed_retrying")
APPLE_REVOCATION = ("pending", "done", "skipped_no_token", "not_required")


class Device(Base):
    """Гостевая техническая запись устройства (план §2.2). E2; account_id — E5; очистка 12 мес. — E8."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint(_in("platform", PLATFORMS), name="ck_devices_platform"),
        CheckConstraint("char_length(app_version) BETWEEN 1 AND 32", name="ck_devices_app_version"),
        Index("ix_devices_account_id", "account_id"),
        Index("ix_devices_last_seen_at", "last_seen_at"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    app_version: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", name="fk_devices_account_id")
    )
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()


class User(Base):
    """Аккаунт (гость аккаунтом не является). E5; state=deleting — E7."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("kind = 'account'", name="ck_users_kind"),
        CheckConstraint(_in("state", USER_STATES), name="ck_users_state"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'account'"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Identity(Base):
    """Провайдер входа → аккаунт, (apple, sub). E5; удаляется последним шагом удаления (§2.5)."""

    __tablename__ = "identities"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "subject", name="pk_identities"),
        CheckConstraint(_in("provider", IDENTITY_PROVIDERS), name="ck_identities_provider"),
        CheckConstraint("char_length(subject) BETWEEN 1 AND 255", name="ck_identities_subject"),
        Index("ix_identities_user_id", "user_id"),
    )

    provider: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()


class RefreshToken(Base):
    """Refresh-токены гостя и аккаунта, только SHA-256 (hex). E2; аккаунт — E5; очистка по expires_at — E8."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_refresh_tokens_token_hash"),
        CheckConstraint(_in("subject_kind", SUBJECT_KINDS), name="ck_refresh_tokens_subject_kind"),
        CheckConstraint(
            "(subject_kind = 'account') = (user_id IS NOT NULL)", name="ck_refresh_tokens_account_user"
        ),
        CheckConstraint(
            f"revoked_reason IS NULL OR {_in('revoked_reason', REVOKED_REASONS)}",
            name="ck_refresh_tokens_revoked_reason",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)", name="ck_refresh_tokens_revoked_pair"
        ),
        CheckConstraint("expires_at > created_at", name="ck_refresh_tokens_expiry"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_device_id", "device_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("devices.device_id", name="fk_refresh_tokens_device_id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class AppleToken(Base):
    """Apple refresh token: AES-GCM ключом приложения, aad = user_id (D13). Только для отзыва. E5, E7."""

    __tablename__ = "apple_tokens"
    __table_args__ = (
        CheckConstraint("octet_length(nonce) = 12", name="ck_apple_tokens_nonce"),
        CheckConstraint("octet_length(ciphertext) > 16", name="ck_apple_tokens_ciphertext"),
        CheckConstraint("key_version > 0", name="ck_apple_tokens_key_version"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", name="fk_apple_tokens_user_id"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class AppleNotification(Base):
    """Уведомления Sign in with Apple: идемпотентность по jti или (sub, type, event_time) (§2.6). E7."""

    __tablename__ = "apple_notifications"
    __table_args__ = (Index("ix_apple_notifications_received_at", "received_at"),)

    event_key: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = _now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRecord(Base):
    """Текущее состояние сущности аккаунта (архитектура §8.1). E6; очистка tombstones 180 дн. — E8."""

    __tablename__ = "sync_records"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "entity_type", "entity_id", name="pk_sync_records"),
        CheckConstraint(_in("entity_type", ENTITY_TYPES), name="ck_sync_records_entity_type"),
        CheckConstraint("version > 0", name="ck_sync_records_version"),
        CheckConstraint("schema_version > 0", name="ck_sync_records_schema_version"),
        # pull по курсору: (user_id, version > cursor) ORDER BY version; версия уникальна у пользователя.
        Index("uq_sync_records_user_version", "user_id", "version", unique=True),
        Index(
            "ix_sync_records_tombstones",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", name="fk_sync_records_user_id")
    )
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    server_updated_at: Mapped[datetime] = _now()


class SyncState(Base):
    """Одна строка на аккаунт: счётчик версий; через её блокировку сериализуются push (план §3.1). E6."""

    __tablename__ = "sync_state"
    __table_args__ = (
        CheckConstraint("last_version >= 0", name="ck_sync_state_last_version"),
        CheckConstraint(
            "tombstone_horizon >= 0 AND tombstone_horizon <= last_version", name="ck_sync_state_horizon"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", name="fk_sync_state_user_id"), primary_key=True
    )
    last_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tombstone_horizon: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = _now()


class SyncMutation(Base):
    """Журнал обработанных мутаций для идемпотентных повторов push. E6; срок 30 дн. — E8."""

    __tablename__ = "sync_mutations"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "mutation_id", name="pk_sync_mutations"),
        Index("ix_sync_mutations_created_at", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", name="fk_sync_mutations_user_id")
    )
    mutation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


class ImportDelivery(Base):
    """Доставка результата разбора (§6.2): повтор по Idempotency-Key. E4; срок 7 дн. — E8."""

    __tablename__ = "import_deliveries"
    __table_args__ = (
        PrimaryKeyConstraint("device_id", "idem_key", name="pk_import_deliveries"),
        Index("uq_import_deliveries_import_id", "import_id", unique=True),
        Index("ix_import_deliveries_created_at", "created_at"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.device_id", name="fk_import_deliveries_device_id")
    )
    idem_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    file_mac: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportLedger(Base):
    """Постоянный реестр подтверждённых импортов (§6.1): только метаданные. E4."""

    __tablename__ = "import_ledger"
    __table_args__ = (
        Index("ix_import_ledger_device_id", "device_id"),
        Index("ix_import_ledger_account_id", "account_id"),
        Index("ix_import_ledger_attributed_account_id", "attributed_account_id"),
    )

    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attributed_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageCounter(Base):
    """Счётчики O(1): subject = device:<uuid> | account:<uuid>. E4."""

    __tablename__ = "usage_counters"
    __table_args__ = (
        PrimaryKeyConstraint("subject", "feature", name="pk_usage_counters"),
        CheckConstraint(
            "subject ~ '^(device|account):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="ck_usage_counters_subject",
        ),
        CheckConstraint(_in("feature", USAGE_FEATURES), name="ck_usage_counters_feature"),
        CheckConstraint("used >= 0", name="ck_usage_counters_used"),
    )

    subject: Mapped[str] = mapped_column(Text)
    feature: Mapped[str] = mapped_column(Text)
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = _now()


class RateCounter(Base):
    """Суточные лимиты частоты (§6.3, IMPORT_RATE_PER_DAY). E2–E3; удаление по expires_at — E8."""

    __tablename__ = "rate_counters"
    __table_args__ = (
        PrimaryKeyConstraint("counter_key", "window_start", name="pk_rate_counters"),
        CheckConstraint("hits >= 0", name="ck_rate_counters_hits"),
        CheckConstraint("expires_at > window_start", name="ck_rate_counters_expiry"),
        Index("ix_rate_counters_expires_at", "expires_at"),
    )

    counter_key: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeletionJob(Base):
    """Задание удаления аккаунта (§2.5): шаг, курсор, аренда, повторы. E7."""

    __tablename__ = "deletion_jobs"
    __table_args__ = (
        CheckConstraint(_in("source", DELETION_SOURCES), name="ck_deletion_jobs_source"),
        CheckConstraint(_in("state", DELETION_STATES), name="ck_deletion_jobs_state"),
        CheckConstraint(_in("apple_revocation", APPLE_REVOCATION), name="ck_deletion_jobs_apple_revocation"),
        CheckConstraint("attempts >= 0", name="ck_deletion_jobs_attempts"),
        Index("ix_deletion_jobs_user_id", "user_id"),
        Index("ix_deletion_jobs_next_run_at", "next_run_at", postgresql_where=text("state <> 'done'")),
        # Повторный DELETE /account → тот же deletion_id: одно незавершённое задание на аккаунт.
        Index(
            "uq_deletion_jobs_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("state <> 'done'"),
        ),
    )

    deletion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_by_did: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    step: Mapped[str] = mapped_column(Text, nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = _now()
    last_error_code: Mapped[str | None] = mapped_column(Text)
    apple_revocation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class DeletedAccount(Base):
    """Удалённые аккаунты: только наш UUID и дата (§2.5). E7; очистка через 400 дней — E8."""

    __tablename__ = "deleted_accounts"
    __table_args__ = (Index("ix_deleted_accounts_deleted_at", "deleted_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime] = _now()
