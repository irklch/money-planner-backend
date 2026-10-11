-- 0001: схема MVP (архитектура §7.1, план §2–§6). {prefix} — папка таблиц окружения.
-- Правило миграций: только идемпотентные операторы (CREATE TABLE IF NOT EXISTS): DDL в YDB не
-- транзакционен, прерванная миграция повторяется целиком. Применённый файл не меняется — изменения
-- схемы идут новой миграцией.

-- Гостевая техническая запись устройства (§2.2). account_id — устройство вошло в аккаунт.
CREATE TABLE IF NOT EXISTS `{prefix}/devices` (
    device_id Utf8 NOT NULL,
    platform Utf8 NOT NULL,
    app_version Utf8 NOT NULL,
    account_id Utf8,
    created_at Timestamp NOT NULL,
    last_seen_at Timestamp NOT NULL,
    PRIMARY KEY (device_id),
    INDEX by_account GLOBAL SYNC ON (account_id)
) WITH (AUTO_PARTITIONING_BY_LOAD = ENABLED);

-- Аккаунт (kind = account; гость аккаунтом не является). state: active | deleting.
CREATE TABLE IF NOT EXISTS `{prefix}/users` (
    user_id Utf8 NOT NULL,
    kind Utf8 NOT NULL,
    state Utf8 NOT NULL,
    created_at Timestamp NOT NULL,
    updated_at Timestamp NOT NULL,
    PRIMARY KEY (user_id)
) WITH (AUTO_PARTITIONING_BY_LOAD = ENABLED);

-- Провайдер входа → аккаунт: (apple, sub). Удаляется последним шагом удаления аккаунта (§2.5).
CREATE TABLE IF NOT EXISTS `{prefix}/identities` (
    provider Utf8 NOT NULL,
    subject Utf8 NOT NULL,
    user_id Utf8 NOT NULL,
    created_at Timestamp NOT NULL,
    PRIMARY KEY (provider, subject),
    INDEX by_user GLOBAL SYNC ON (user_id)
);

-- Refresh-токены (только хэш). subject_kind: guest | account; user_id — у аккаунта.
-- revoked_reason: session_revoked | account_deleted | reuse_detected | logout. Очистка по expires_at — E8.
CREATE TABLE IF NOT EXISTS `{prefix}/refresh_tokens` (
    token_hash Utf8 NOT NULL,
    family_id Utf8 NOT NULL,
    subject_kind Utf8 NOT NULL,
    device_id Utf8 NOT NULL,
    user_id Utf8,
    created_at Timestamp NOT NULL,
    expires_at Timestamp NOT NULL,
    rotated_at Timestamp,
    revoked_at Timestamp,
    revoked_reason Utf8,
    PRIMARY KEY (token_hash),
    INDEX by_family GLOBAL SYNC ON (family_id),
    INDEX by_user GLOBAL SYNC ON (user_id)
) WITH (AUTO_PARTITIONING_BY_LOAD = ENABLED);

-- Apple refresh token аккаунта: шифротекст KMS (aad = user_id), только для отзыва (§2.5).
CREATE TABLE IF NOT EXISTS `{prefix}/apple_tokens` (
    user_id Utf8 NOT NULL,
    ciphertext String NOT NULL,
    kms_key_id Utf8 NOT NULL,
    created_at Timestamp NOT NULL,
    updated_at Timestamp NOT NULL,
    PRIMARY KEY (user_id)
);

-- Уведомления Sign in with Apple: идемпотентность по jti или (sub, type, event_time) (§2.6).
CREATE TABLE IF NOT EXISTS `{prefix}/apple_notifications` (
    event_key Utf8 NOT NULL,
    event_type Utf8 NOT NULL,
    received_at Timestamp NOT NULL,
    processed_at Timestamp,
    PRIMARY KEY (event_key)
);

-- Sync (архитектура §8.1; прототип prototype/ydb-sync/syncproto/schema.py, проверено в облаке).
-- by_version — обязательно GLOBAL SYNC: ASYNC-индекс eventual и ломает pull по курсору.
CREATE TABLE IF NOT EXISTS `{prefix}/sync_records` (
    user_id Utf8 NOT NULL,
    entity_type Utf8 NOT NULL,
    entity_id Utf8 NOT NULL,
    version Uint64 NOT NULL,
    created_at Timestamp NOT NULL,
    updated_at Timestamp NOT NULL,
    deleted_at Timestamp,
    order_ts Timestamp NOT NULL,
    device_id Utf8 NOT NULL,
    schema_version Uint32 NOT NULL,
    mutation_id Utf8 NOT NULL,
    payload Json,
    server_updated_at Timestamp NOT NULL,
    PRIMARY KEY (user_id, entity_type, entity_id),
    INDEX by_version GLOBAL SYNC ON (user_id, version)
        COVER (created_at, updated_at, deleted_at, order_ts, device_id, schema_version, mutation_id,
               payload, server_updated_at)
) WITH (
    AUTO_PARTITIONING_BY_SIZE = ENABLED,
    AUTO_PARTITIONING_BY_LOAD = ENABLED
);

-- Одна строка на аккаунт: через неё сериализуются конкурентные push (OCC).
CREATE TABLE IF NOT EXISTS `{prefix}/sync_state` (
    user_id Utf8 NOT NULL,
    last_version Uint64 NOT NULL,
    tombstone_horizon Uint64 NOT NULL,
    updated_at Timestamp NOT NULL,
    PRIMARY KEY (user_id)
) WITH (AUTO_PARTITIONING_BY_LOAD = ENABLED);

-- Журнал обработанных мутаций для идемпотентных повторов push. TTL 30 дней.
CREATE TABLE IF NOT EXISTS `{prefix}/sync_mutations` (
    user_id Utf8 NOT NULL,
    mutation_id Utf8 NOT NULL,
    request_hash Utf8 NOT NULL,
    result Json NOT NULL,
    created_at Timestamp NOT NULL,
    PRIMARY KEY (user_id, mutation_id)
) WITH (
    AUTO_PARTITIONING_BY_LOAD = ENABLED,
    TTL = Interval("P30D") ON created_at
);

-- Доставки результатов разбора (§6.2). import_id — UUIDv7. TTL 7 дней.
CREATE TABLE IF NOT EXISTS `{prefix}/import_deliveries` (
    device_id Utf8 NOT NULL,
    idem_key Utf8 NOT NULL,
    import_id Utf8 NOT NULL,
    file_mac Utf8 NOT NULL,
    account_id Utf8,
    created_at Timestamp NOT NULL,
    confirmed_at Timestamp,
    PRIMARY KEY (device_id, idem_key),
    INDEX by_import GLOBAL SYNC ON (import_id)
) WITH (TTL = Interval("P7D") ON created_at);

-- Постоянный реестр подтверждённых импортов (§6.1): только метаданные.
CREATE TABLE IF NOT EXISTS `{prefix}/import_ledger` (
    import_id Utf8 NOT NULL,
    device_id Utf8 NOT NULL,
    account_id Utf8,
    attributed_account_id Utf8,
    confirmed_at Timestamp NOT NULL,
    PRIMARY KEY (import_id),
    INDEX by_device GLOBAL SYNC ON (device_id),
    INDEX by_account GLOBAL SYNC ON (account_id),
    INDEX by_attributed_account GLOBAL SYNC ON (attributed_account_id)
);

-- Счётчики O(1): subject = device:<id> | account:<id>, feature = imports.
CREATE TABLE IF NOT EXISTS `{prefix}/usage_counters` (
    subject Utf8 NOT NULL,
    feature Utf8 NOT NULL,
    used Uint64 NOT NULL,
    updated_at Timestamp NOT NULL,
    PRIMARY KEY (subject, feature)
);

-- Лимиты частоты между экземплярами (§6.3, архитектура §10.1). Удаляются по TTL.
CREATE TABLE IF NOT EXISTS `{prefix}/rate_counters` (
    counter_key Utf8 NOT NULL,
    window_start Timestamp NOT NULL,
    hits Uint32 NOT NULL,
    expires_at Timestamp NOT NULL,
    PRIMARY KEY (counter_key, window_start)
) WITH (
    AUTO_PARTITIONING_BY_LOAD = ENABLED,
    TTL = Interval("PT0S") ON expires_at
);

-- Задания удаления аккаунта (§2.5): шаг, курсор, аренда, повторы.
-- state: pending | revoking | deleting | done | failed_retrying; source: user | apple_notification.
-- apple_revocation: pending | done | skipped_no_token | not_required.
CREATE TABLE IF NOT EXISTS `{prefix}/deletion_jobs` (
    deletion_id Utf8 NOT NULL,
    user_id Utf8 NOT NULL,
    requested_by_did Utf8,
    source Utf8 NOT NULL,
    state Utf8 NOT NULL,
    step Utf8 NOT NULL,
    cursor Utf8,
    attempts Uint32 NOT NULL,
    lease_until Timestamp,
    next_run_at Timestamp NOT NULL,
    last_error_code Utf8,
    apple_revocation Utf8 NOT NULL,
    created_at Timestamp NOT NULL,
    updated_at Timestamp NOT NULL,
    PRIMARY KEY (deletion_id),
    INDEX by_user GLOBAL SYNC ON (user_id)
);

-- Удалённые аккаунты: только наш UUID и дата (§2.5). Очистка через 400 дней — E8.
CREATE TABLE IF NOT EXISTS `{prefix}/deleted_accounts` (
    user_id Utf8 NOT NULL,
    deleted_at Timestamp NOT NULL,
    PRIMARY KEY (user_id)
);
