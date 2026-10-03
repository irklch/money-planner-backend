import re
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MODEL_URI_RE = re.compile(r"^gpt://[A-Za-z0-9_-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")


class Settings(BaseSettings):
    """Вся конфигурация — из переменных окружения (в проде их заполняет Lockbox, см. core/secrets.py)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="dev", pattern="^(dev|test|prod)$")
    log_level: str = "INFO"

    # --- База данных ---
    database_url: SecretStr = SecretStr("postgresql+asyncpg://money:money@localhost:5432/money")
    db_ssl_root_cert: str | None = None  # путь к CA Yandex Managed PostgreSQL
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- Токены ---
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    # Схема БД: «≥ 12 мес, продлевается при использовании». Промпт этапа: 30 дней — открытый вопрос.
    refresh_token_ttl_days: int = 365
    refresh_grace_seconds: int = 60
    anonymous_idempotency_window_hours: int = 24
    # Секрет для детерминированных производных значений (refresh-токены при grace/replay).
    token_derivation_secret: SecretStr = SecretStr("dev-only-derivation-secret-change-me")

    # --- Даты ---
    # Сервер не знает часовой пояс клиента: «сегодня» = дата в самом восточном поясе (UTC+14),
    # чтобы не отклонять корректную дату пользователя. Открытый вопрос.
    max_client_utc_offset_hours: int = 14

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
        if problems:
            raise RuntimeError(f"Production config is incomplete: {', '.join(problems)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
