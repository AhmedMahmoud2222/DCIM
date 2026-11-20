import json
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core import secret_policy


def _database_password_category(url: str) -> str | None:
    """Why the database URL's password is unacceptable in production, or None.

    Two independent parsers must agree on the credentials: SQLAlchemy's (what the engine really connects
    with) and a strict RFC 3986 split that confines userinfo to the authority and ignores any '@' in the
    path, query or fragment. A `?password=` query value takes precedence, as in the asyncpg/psycopg connect
    arguments. A disagreement means the URL is ambiguous and is rejected rather than guessed at."""
    try:
        parsed = make_url(url)
        from_query = parsed.query.get("password")
        engine_password = parsed.password
        if from_query is not None:
            engine_password = from_query[-1] if isinstance(from_query, tuple) else from_query
        split = urlsplit(url)
        strict_query = parse_qs(split.query).get("password")
        strict_password = strict_query[-1] if strict_query else (unquote(split.password) if split.password is not None else None)
    except (ArgumentError, ValueError):
        return "not parseable as a database URL"
    for password in (engine_password, strict_password):
        category = secret_policy.check_database_password(password)
        if category:
            return category
    if engine_password != strict_password:
        return "ambiguous credentials (URL parsers disagree)"
    return None


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo configured secrets (JWT key, credential
    # encryption keys) back into logs or tracebacks (SEC-02, Issue #38).
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True)

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
    # SEC-02 (Issue #38): additional key generations for online rotation, as a JSON object
    # mapping "v<N>" (N >= 2) to a Fernet key, e.g. '{"v2": "<key>"}'. Precedence:
    # `credential_encryption_key` is ALWAYS version v1 (so every existing deployment that
    # sets only that variable keeps working unchanged, and unprefixed legacy ciphertext
    # keeps decrypting); this map only ADDS v2 and later and may not redefine v1. New
    # encryptions use the highest version configured. Every key is checked to be a valid
    # Fernet key when this map is set; errors name the version, never the key value.
    credential_encryption_keys: str | None = None

    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Pre-MVP consolidation hardening (Codex H1 / SSRF, docs/history/PRE_MVP_CONSOLIDATION_REPORT.md):
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

    # Issue #103: outbound notification webhooks and ITSM calls share one deny-by-default policy. The
    # allowlist is empty by default, so no provider is reachable until an operator names its network.
    # HTTPS only unless outbound_allow_http is set (development and tests); redirects are never followed
    # and ambient proxy variables are ignored (see app/application/outbound_http.py).
    outbound_allowed_networks: list[str] = Field(default_factory=list)
    outbound_allowed_ports: list[int] = Field(default_factory=lambda: [443])
    outbound_allow_http: bool = False
    outbound_allow_loopback: bool = False
    outbound_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    outbound_max_response_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)

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

    # DB-03 (#357): idempotency ledger lifetime. User endpoints keep a fixed 24 h window (idempotency.py);
    # collector ingestion is configurable and MUST exceed the Edge Collector's own replay window
    # (queue retention_seconds, default 24 h, plus the 300 s maximum retry delay) with a margin. Central
    # cannot read the edge value, so the 36 h floor is a guard, not proof: confirm the effective edge
    # retention at every deployment (docs/implementation/IDEMPOTENCY_EXPIRY.md).
    collector_idempotency_ttl_hours: int = Field(default=72, ge=36)

    # DB-03 purge of expired idempotency rows. DISABLED unless an operator explicitly authorizes it;
    # while disabled the task returns without opening a database session or issuing SQL.
    idempotency_purge_enabled: bool = False
    idempotency_purge_grace_seconds: int = Field(default=3600, ge=3600)
    idempotency_purge_batch_size: int = Field(default=500, ge=1, le=5000)
    idempotency_purge_max_runtime_seconds: float = Field(default=30.0, gt=0, le=240)
    idempotency_purge_pause_ms: int = Field(default=50, ge=0, le=5000)
    idempotency_purge_statement_timeout_ms: int = Field(default=2000, ge=1, le=2000)

    api_v1_prefix: str = "/api/v1"

    log_level: str = "INFO"

    # Phase 10A PR-5: local-filesystem-backed storage for catalog revision graphics
    # (front/rear equipment photos). Relative paths resolve against the process's
    # working directory (matching how `.env`/`alembic.ini` are already located there);
    # an absolute path is used as-is. See app/infrastructure/storage/.
    catalog_graphics_storage_root: str = "media/catalog/graphics"
    catalog_graphics_max_upload_bytes: int = 10 * 1024 * 1024

    # G9a DRAFT prototype only. Default publication stays legacy-compatible.
    # Applies to newly published equipment only: rack weight currently bridges to
    # load capacity, whose semantics/rounding require a separate owner decision.
    catalog_decimal_publication_projection: bool = False

    # Issue #365: precise site coordinates on GET /dashboard/geography are withheld from callers with a
    # restricted (non-global) scope by default. Setting this to true is an explicit operator authorization
    # to return latitude/longitude of in-scope sites to restricted callers too; it never widens scope.
    geography_restricted_coordinates_enabled: bool = False

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
    # Filesystem policy (Landlock) for every extraction child: no read access to the worker's process
    # environment or the media volume. Only turn off on a development host that has no Landlock.
    catalog_extraction_require_landlock: bool = True
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
    def _validate_credential_encryption_keys(self) -> "Settings":
        self.credential_encryption_key_versions()
        return self

    def credential_encryption_key_versions(self) -> dict[int, str]:
        """Every configured credential-encryption key by version number (1 is
        `credential_encryption_key`). Raises ValueError, naming the version but never the
        key, when `credential_encryption_keys` is malformed or holds an invalid key."""
        versions = {1: self.credential_encryption_key}
        raw = (self.credential_encryption_keys or "").strip()
        if not raw:
            # Unchanged single-key behaviour: v1 keeps only its historical min_length check.
            return versions
        try:
            extra = json.loads(raw)
        except ValueError:
            raise ValueError("credential_encryption_keys must be a JSON object such as {\"v2\": \"<key>\"}.") from None
        if not isinstance(extra, dict) or not extra:
            raise ValueError("credential_encryption_keys must be a non-empty JSON object such as {\"v2\": \"<key>\"}.")
        for label, key in extra.items():
            match = re.fullmatch(r"v([1-9][0-9]{0,8})", label) if isinstance(label, str) else None
            if match is None:
                raise ValueError(f"credential_encryption_keys: version labels must look like 'v2'; got {label!r}.")
            number = int(match.group(1))
            if number == 1:
                raise ValueError("credential_encryption_keys may not define v1; v1 is always CREDENTIAL_ENCRYPTION_KEY.")
            if not isinstance(key, str):
                raise ValueError(f"credential_encryption_keys: the key for {label} must be a string.")
            versions[number] = key
        for number, key in sorted(versions.items()):
            try:
                Fernet(key.encode())
            except (ValueError, TypeError):
                raise ValueError(f"Credential encryption key v{number} is not a valid Fernet key.") from None
        return versions

    @model_validator(mode="after")
    def _production_rejects_insecure_secrets(self) -> "Settings":
        """DB-08 (Issue #359): fail closed in production on empty, example or placeholder secrets.

        The contract lives in app.core.secret_policy and is shared with the deployment preflight.
        Messages name variables and categories only; no configured value is ever included."""
        if self.environment != "production":
            return self
        findings: list[tuple[str, str]] = []
        category = secret_policy.classify_secret(self.jwt_secret_key)
        if category:
            findings.append(("JWT_SECRET_KEY", category))
        category = _database_password_category(self.database_url)
        if category:
            findings.append(("DATABASE_URL password", category))
        # v1 has only its historical min_length check outside production. Fernet() is what every
        # encrypt/decrypt calls, so a v1 key that ever sealed a row passes this identical rule.
        if not secret_policy.is_valid_fernet_key(self.credential_encryption_key):
            findings.append(("CREDENTIAL_ENCRYPTION_KEY", secret_policy.INVALID_FERNET))
        if findings:
            detail = secret_policy.format_findings(findings)
            raise ValueError(f"Insecure configuration rejected for ENVIRONMENT=production (values withheld): {detail}")
        return self

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
