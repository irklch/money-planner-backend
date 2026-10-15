import re
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MODEL_URI_RE = re.compile(r"^gpt://[A-Za-z0-9_-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")


class Settings(BaseSettings):
    """Вся конфигурация — из переменных окружения (deploy/portable: .env на сервере, D24)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="dev", pattern="^(dev|test|prod)$")
    log_level: str = "INFO"

    # --- База данных ---
    database_url: SecretStr = SecretStr("postgresql+asyncpg://money:money@localhost:5432/money")
    # Либо DATABASE_URL целиком, либо части (deploy/portable): URL собирается через SQLAlchemy,
    # поэтому спецсимволы пароля (/ + = @ : % ? &) кодируются корректно.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_ssl_root_cert: str | None = None  # CA внешнего PostgreSQL; в deploy/portable БД в сети Compose
    db_pool_size: int = 10
    db_max_overflow: int = 5
    # /healthz: проверка готовности БД (SELECT 1) с таймаутом; при отказе — 503 + Retry-After.
    db_health_timeout_s: float = Field(default=2.0, gt=0, le=30)
    db_unavailable_retry_after_s: int = Field(default=5, ge=1, le=300)

    # --- Токены ---
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    # Константы протокола (contract/config-parameters.json → protocol: ACCESS_TOKEN_TTL_S,
    # REFRESH_TOKEN_TTL_DAYS, REFRESH_GRACE_S, GUEST_IDEMPOTENCY_WINDOW_H; совпадение проверяется тестом).
    # Меняются только новой версией контракта; переопределение из env — для тестов.
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 365  # срок каждого refresh-токена; ротация выдаёт новый на 365 дней
    refresh_grace_seconds: int = 60
    guest_idempotency_window_hours: int = 24
    # Секрет для детерминированных производных значений (refresh-токены при grace/replay).
    token_derivation_secret: SecretStr = SecretStr("dev-only-derivation-secret-change-me")

    # --- Импорт и квоты: параметры плана §6.3 (значения по умолчанию = contract/config-parameters.json) ---
    # Читаются с E1; применяются этапами E2–E4 (разбор, учёт импортов, лимиты). До E3 старый разбор
    # использует параметры ниже («Старый импорт»).
    import_max_file_bytes: int = Field(default=2_097_152, ge=1)
    import_max_operations: int = Field(default=5000, ge=1)
    import_parse_timeout_s: int = Field(default=30, ge=1, le=300)
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

    # --- Старый импорт (до E3) ---
    max_upload_bytes: int = 10 * 1024 * 1024
    max_import_expenses: int = 5000
    max_import_body_bytes: int = 2 * 1024 * 1024
    parse_concurrency_global: int = 4
    parse_concurrency_per_user: int = 1

    # --- Rate limits (в памяти процесса; MVP — один инстанс) ---
    # POST /auth/refresh: rate_auth_per_minute × 3 на IP (защита из старого кода; в контракте —
    # только ответ 429). Создание гостевых записей — GUEST_CREATE_RATE_PER_IP выше.
    rate_auth_per_minute: int = 10
    rate_parse_per_minute: int = 10
    rate_import_per_minute: int = 20
    rate_insights_per_minute: int = 20

    # --- Категории ---
    category_palette_size: int = 12

    # --- ИИ (Yandex AI Studio) ---
    ai_enabled: bool = False
    ai_folder_id: str | None = None
    ai_api_key: SecretStr | None = None  # API-ключ сервисного аккаунта AI Studio (без metadata-IAM, D24)
    ai_base_url: str = "https://llm.api.cloud.yandex.net"
    ai_model_categorize: str | None = None  # gpt://<folder>/yandexgpt-lite/latest
    ai_model_insights: str | None = None  # gpt://<folder>/yandexgpt/latest
    ai_timeout_seconds: float = 20.0
    ai_categorize_batch_size: int = 60

    @model_validator(mode="after")
    def _database_url_from_parts(self) -> "Settings":
        parts = {
            "DB_HOST": self.db_host,
            "DB_NAME": self.db_name,
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
        }
        if not any(v is not None for v in parts.values()):
            return self
        if "database_url" in self.model_fields_set:
            raise ValueError("set either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD, not both")
        missing = [k for k, v in parts.items() if v is None]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
        self.database_url = SecretStr(url.render_as_string(hide_password=False))
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
        if self.ai_enabled and not (
            self.ai_model_categorize and self.ai_model_insights and self.ai_folder_id
        ):
            problems.append("AI_* model configuration")
        if self.ai_enabled and self.ai_api_key is None:
            problems.append("AI_API_KEY (metadata IAM is not supported, D24)")
        if problems:
            raise RuntimeError(f"Production config is incomplete: {', '.join(problems)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
