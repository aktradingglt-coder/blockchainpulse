"""Process settings from environment variables (PROMPT §19). Secrets are ``SecretStr`` and never logged."""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class DeploymentMode(StrEnum):
    HOSTED = "hosted"
    SELF_HOSTED = "self_hosted"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "product.yaml").is_file():
            return parent
    return Path.cwd()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, frozen=True, hide_input_in_errors=True
    )

    app_env: AppEnv = AppEnv.DEV
    app_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:3000")
    api_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    deployment_mode: DeploymentMode = DeploymentMode.HOSTED
    config_dir: Path = Field(default_factory=lambda: _repo_root() / "config")

    database_url: PostgresDsn = PostgresDsn("postgresql+asyncpg://rdesk@localhost:5432/rdesk")
    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    service_name: str = "rd-unknown"

    anthropic_api_key: SecretStr | None = None
    sec_edgar_user_agent: str | None = None
    fred_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None
    billing_provider: str | None = None
    billing_api_key: SecretStr | None = None
    billing_webhook_secret: SecretStr | None = None
    email_provider_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    auth_secret: SecretStr | None = None
    licence_signing_private_key: SecretStr | None = None
    sentry_dsn: SecretStr | None = None
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None

    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    feature_flags: Annotated[dict[str, bool], NoDecode] = Field(default_factory=dict)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:  # noqa: ANN401 - pydantic validator signature
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("feature_flags", mode="before")
    @classmethod
    def _parse_flags(cls, value: Any) -> Any:  # noqa: ANN401 - pydantic validator signature
        if isinstance(value, str):
            if not value.strip():
                return {}
            parsed = json.loads(value)
            if not isinstance(parsed, dict) or not all(isinstance(v, bool) for v in parsed.values()):
                raise ValueError("FEATURE_FLAGS must be a JSON object of booleans")
            return parsed
        return value

    @field_validator("database_url")
    @classmethod
    def _asyncpg_only(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return value

    @model_validator(mode="after")
    def _strict_cors(self) -> Settings:
        for origin in self.cors_allowed_origins:
            if origin == "*" or "*" in origin:
                raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins; wildcards are not allowed")
            if self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION) and not origin.startswith("https://"):
                raise ValueError("CORS origins must use https:// in staging and production")
        return self

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {value!r}")
        return upper

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
