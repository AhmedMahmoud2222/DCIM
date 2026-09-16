from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"

    database_url: str = Field(..., description="Async SQLAlchemy URL, e.g. postgresql+asyncpg://...")
    database_pool_size: int = 10
    database_max_overflow: int = 5

    redis_url: str = Field(..., description="e.g. redis://localhost:6379/0")

    jwt_secret_key: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    api_v1_prefix: str = "/api/v1"

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        PostgresDsn(v.replace("postgresql+asyncpg", "postgresql").replace("postgresql+psycopg", "postgresql"))
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
