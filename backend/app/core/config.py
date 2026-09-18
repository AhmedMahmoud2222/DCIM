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

    # Phase 8: Fernet key for Integration credential-at-rest encryption
    # (app/core/secrets.py). A urlsafe-base64-encoded 32-byte key, e.g. the output of
    # `Fernet.generate_key()`. See app/core/secrets.py's own docstring: this is an
    # explicit OPEN DECISION for production KMS migration, not a finished design.
    credential_encryption_key: str = Field(..., min_length=32)

    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Pre-MVP consolidation hardening (Codex H1 / SSRF, PRE_MVP_CONSOLIDATION_REPORT.md):
    # the DCIM-specific allowlist of network ranges a REST integration is permitted to
    # poll -- e.g. ["10.10.0.0/16", "172.20.10.0/24"]. Deliberately empty by default
    # (deny-by-default): an operator must explicitly configure the private ranges
    # their own DCIM devices (UPS/PDU/BMS gateways/sensors/network devices/management
    # controllers) actually live in. See app/application/drivers/network_policy.py.
    rest_integration_allowed_networks: list[str] = Field(default_factory=list)
    rest_integration_allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    # Test/development-only escape hatch -- must never be true in a production
    # deployment's configuration. Lets a REST integration target 127.0.0.1/::1.
    rest_integration_allow_loopback: bool = False

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
