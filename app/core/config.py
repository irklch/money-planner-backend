import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Хосты локальной YDB: анонимный доступ разрешён только к ним.
LOCAL_YDB_HOSTS = {"localhost", "127.0.0.1", "::1", "ydb"}
_YDB_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
_MODEL_URI_RE = re.compile(r"^gpt://[A-Za-z0-9_-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")


class Settings(BaseSettings):
    """Вся конфигурация — из переменных окружения (в проде их заполняет Lockbox, см. core/secrets.py)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="dev", pattern="^(dev|test|prod)$")
    log_level: str = "INFO"

    # --- YDB (новый backend, план E1) ---
    # Без YDB_ENDPOINT слой YDB не подключается (старые модули на PostgreSQL до E2–E7). В prod обязателен.
    ydb_endpoint: str | None = None
    ydb_database: str = "/local"
    # anonymous — только локальная YDB; metadata — IAM сервисного аккаунта (Serverless Containers);
    # env — YDB_ACCESS_TOKEN_CREDENTIALS и т. п. (миграции оператором).
    ydb_auth: Literal["anonymous", "metadata", "env"] = "anonymous"
    # Папка таблиц внутри базы: разные префиксы у приложения и тестов.
    ydb_table_prefix: str = "mp"
    # План §3.1: 1–2 сессии на экземпляр (RU компиляции запросов на новых сессиях).
    ydb_pool_size: int = Field(default=2, ge=1, le=2)
    ydb_connect_timeout_s: float = Field(default=10.0, gt=0, le=60)
    # Таймаут одной попытки запроса и общий бюджет повторов операции.
    ydb_query_timeout_s: float = Field(default=5.0, gt=0, le=60)
    ydb_retry_max_attempts: int = Field(default=5, ge=1, le=20)
    ydb_retry_budget_s: float = Field(default=10.0, gt=0, le=60)
    # Retry-After (с) в ответе 503, когда YDB недоступна или перегружена после всех повторов.
    ydb_unavailable_retry_after_s: int = Field(default=1, ge=1, le=60)

    # --- Импорт и квоты: параметры плана §6.3 (contract/config-parameters.json → env) ---
    import_max_file_bytes: int = Field(default=2_097_152, ge=1, le=2_621_440)
    import_max_operations: int = Field(default=5000, ge=1)
    import_parse_timeout_s: int = Field(default=30, ge=1, le=60)
    import_rate_per_minute: int = Field(default=5, ge=1)
    import_rate_per_day: int = Field(default=10, ge=1)
    import_concurrency_per_device: int = Field(default=1, ge=1)
    import_concurrency_per_instance: int = Field(default=4, ge=1)
    import_unconfirmed_max: int = Field(default=3, ge=1)
    import_free_limit: int = Field(default=3, ge=0)
    enforce_quotas: bool = False
    ai_categorization_enabled: bool = True
    ai_max_descriptions_per_import: int = Field(default=500, ge=0)
    guest_create_rate_per_ip: int = Field(default=10, ge=1)

    # --- База данных PostgreSQL (старые модули; удаляется в E11) ---
    database_url: SecretStr = SecretStr("postgresql+asyncpg://money:money@localhost:5432/money")
    db_ssl_root_cert: str | None = None  # путь к CA Yandex Managed PostgreSQL
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- Токены ---
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    # Схема БД: 12 мес, продлевается ротацией при каждом использовании.
    refresh_token_ttl_days: int = 365
    refresh_grace_seconds: int = 60
    anonymous_idempotency_window_hours: int = 24
    # Секрет для детерминированных производных значений (refresh-токены при grace/replay).
    token_derivation_secret: SecretStr = SecretStr("dev-only-derivation-secret-change-me")

    # --- Импорт ---
    max_upload_bytes: int = 10 * 1024 * 1024
    max_import_expenses: int = 5000
    max_import_body_bytes: int = 2 * 1024 * 1024
    parse_concurrency_global: int = 4
    parse_concurrency_per_user: int = 1

    # --- Rate limits (в памяти процесса; MVP — один инстанс) ---
    rate_auth_per_minute: int = 10
    rate_parse_per_minute: int = 10
    rate_import_per_minute: int = 20
    rate_insights_per_minute: int = 20

    # --- Категории ---
    category_palette_size: int = 12

    # --- ИИ (Yandex AI Studio) ---
    ai_enabled: bool = False
    ai_folder_id: str | None = None
    ai_api_key: SecretStr | None = None  # либо API-ключ, либо IAM-токен сервисного аккаунта VM
    ai_use_metadata_iam: bool = False
    ai_base_url: str = "https://llm.api.cloud.yandex.net"
    ai_model_categorize: str | None = None  # gpt://<folder>/yandexgpt-lite/latest
    ai_model_insights: str | None = None  # gpt://<folder>/yandexgpt/latest
    ai_timeout_seconds: float = 20.0
    ai_categorize_batch_size: int = 60

    # --- Lockbox ---
    lockbox_secret_id: str | None = None

    @field_validator("ydb_table_prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _YDB_PREFIX_RE.match(v):
            raise ValueError("YDB_TABLE_PREFIX: латиница, цифры, _ и -, сегменты через /")
        return v

    @model_validator(mode="after")
    def _check_ydb_auth(self) -> "Settings":
        # Анонимный доступ — только к локальной YDB в Docker.
        if self.ydb_endpoint and self.ydb_auth == "anonymous":
            host = urlparse(self.ydb_endpoint).hostname or ""
            if host not in LOCAL_YDB_HOSTS:
                raise ValueError("YDB_AUTH=anonymous разрешён только для локальной YDB")
        return self

    @field_validator("ai_model_categorize", "ai_model_insights")
    @classmethod
    def _check_model_uri(cls, v: str | None) -> str | None:
        if v is not None and not _MODEL_URI_RE.match(v):
            raise ValueError("model URI must look like gpt://<folder-id>/<model>[/<version>]")
        return v

    def check_production_ready(self) -> None:
        if self.env != "prod":
            return
        problems = []
        if "dev-only" in self.jwt_secret.get_secret_value():
            problems.append("JWT_SECRET")
        if "dev-only" in self.token_derivation_secret.get_secret_value():
            problems.append("TOKEN_DERIVATION_SECRET")
        if len(self.jwt_secret.get_secret_value()) < 32:
            problems.append("JWT_SECRET too short")
        if not self.ydb_endpoint:
            problems.append("YDB_ENDPOINT")
        elif not self.ydb_endpoint.startswith("grpcs://"):
            problems.append("YDB_ENDPOINT must use TLS (grpcs://)")
        if self.ydb_auth != "metadata":
            problems.append("YDB_AUTH must be metadata")
        if self.ai_enabled and not (
            self.ai_model_categorize and self.ai_model_insights and self.ai_folder_id
        ):
            problems.append("AI_* model configuration")
        if problems:
            raise RuntimeError(f"Production config is incomplete: {', '.join(problems)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
