from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
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

    # MVP monitoring-policy defaults. Retention is a controlled job, never an
    # immediate destructive side effect of changing configuration.
    default_poll_interval_seconds: int = 300
    telemetry_raw_retention_days: int = 365
    telemetry_daily_retention_days: int | None = None
    alarm_history_retention_days: int | None = None

    # SEC-05: only longer retention is configurable without a new owner policy.
    # The worker, not the API or beat process, applies these settings at run time.
    nonce_retention_seconds: int = Field(default=3600, ge=3600)
    heartbeat_retention_days: int = Field(default=30, ge=30)

    api_v1_prefix: str = "/api/v1"

    log_level: str = "INFO"

    # Phase 10A PR-5: local-filesystem-backed storage for catalog revision graphics
    # (front/rear equipment photos). Relative paths resolve against the process's
    # working directory (matching how `.env`/`alembic.ini` are already located there);
    # an absolute path is used as-is. See app/infrastructure/storage/.
    catalog_graphics_storage_root: str = "media/catalog/graphics"
    catalog_graphics_max_upload_bytes: int = 10 * 1024 * 1024

    # DCIM01 PDF datasheet import (docs/plans/DCIM01_PDF_DATASHEET_IMPORT_PLAN_v2.md).
    # Every limit is configurable; the defaults are the approved values.
    catalog_documents_storage_root: str = "media/catalog/documents"
    catalog_document_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    catalog_document_max_pages: int = Field(default=100, ge=1)
    catalog_document_staging_retention_days: int = Field(default=14, ge=1)
    # `required` fails closed: an unreachable scanner rejects the upload. `optional` scans when
    # the scanner answers and records `scan_status='skipped'` when it does not. `off` never
    # scans. Production refuses to start with anything but `required` (see the validator).
    catalog_pdf_scan_mode: Literal["required", "optional", "off"] = "required"
    clamd_host: str = "clamav"
    clamd_port: int = 3310
    clamd_timeout_seconds: float = Field(default=30.0, gt=0)

    # DCIM01 PDF datasheet import, PR-B: candidate extraction. Native text first; OCR only for pages
    # with (almost) no native text. Everything that touches the PDF runs in a sandboxed child with
    # these limits (see app/application/catalog_documents/extraction/sandbox.py).
    catalog_extraction_native_wall_seconds: float = Field(default=45.0, gt=0)
    catalog_extraction_native_cpu_seconds: int = Field(default=30, ge=1)
    catalog_extraction_address_space_bytes: int = Field(default=1024 * 1024 * 1024, ge=64 * 1024 * 1024)
    catalog_extraction_max_page_chars: int = Field(default=100_000, ge=1000)
    catalog_extraction_max_total_chars: int = Field(default=1_500_000, ge=1000)
    catalog_extraction_analysis_wall_seconds: float = Field(default=30.0, gt=0)
    catalog_extraction_lease_seconds: int = Field(default=300, ge=30)
    catalog_extraction_max_attempts: int = Field(default=3, ge=1)
    catalog_ocr_enabled: bool = True
    catalog_ocr_tesseract_path: str = "/usr/bin/tesseract"
    catalog_ocr_language: str = Field(default="eng", pattern=r"^[a-z]{3}(\+[a-z]{3}){0,2}$")
    catalog_ocr_max_pages: int = Field(default=20, ge=0)
    catalog_ocr_page_timeout_seconds: int = Field(default=60, ge=1)
    catalog_ocr_max_image_pixels: int = Field(default=40_000_000, ge=1)
    catalog_ocr_min_native_chars: int = Field(default=25, ge=0)

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        PostgresDsn(v.replace("postgresql+asyncpg", "postgresql").replace("postgresql+psycopg", "postgresql"))
        return v

    @model_validator(mode="after")
    def _production_requires_malware_scanning(self) -> "Settings":
        if self.environment == "production" and self.catalog_pdf_scan_mode != "required":
            raise ValueError("catalog_pdf_scan_mode must be 'required' when ENVIRONMENT=production.")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
