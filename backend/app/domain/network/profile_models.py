"""Issue #101: data-driven network profiles.

Chain: Protocol -> Driver -> VendorProfile -> DeviceProfile -> MetricMapping / Discovery.

* `VendorProfile` holds vendor-level behaviour: enterprise sysObjectID prefixes, the
  protocols and neighbor-discovery protocols the vendor family supports, default
  discovery OIDs and reusable metric definitions.
* `DeviceProfile` is bound to exactly one `VendorProfile` and narrows it to a model or
  family through declarative match criteria, optional firmware bounds, interface
  discovery strategy and neighbor-discovery behaviour.
* `ProfileMetricMapping` rows are the OID -> canonical metric definitions owned by either
  a vendor (reusable defaults) or a device profile (override by canonical metric).

No vendor-specific OID is hard-coded in services: they are rows/JSON here, validated by
`app/application/network/profile_schema.py` before they are stored. Profiles never hold
secrets; SNMP credentials stay in `Integration.credential_ciphertext`.

Lifecycle: `active` -> `retired` (terminal). Retired profiles are kept for provenance but
are never matched and never bound to new integrations. `version` is an optimistic
concurrency counter bumped on every content change.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

PROFILE_STATUSES = ("active", "retired")
DEVICE_CLASSES = ("switch", "router", "firewall", "pdu", "ups", "server", "storage", "sensor", "generic")
METRIC_VALUE_TYPES = ("gauge", "counter", "string")
PROFILE_CODE_PATTERN = "^[a-z0-9][a-z0-9._-]{1,63}$"


class VendorProfile(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "vendor_profile"
    __table_args__ = (
        CheckConstraint(f"status IN {PROFILE_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"code ~ '{PROFILE_CODE_PATTERN}'", name="code_format"),
        CheckConstraint("jsonb_typeof(sys_object_id_prefixes) = 'array'", name="prefixes_is_array"),
        CheckConstraint("jsonb_typeof(supported_protocols) = 'array'", name="protocols_is_array"),
        CheckConstraint("jsonb_typeof(discovery_oids) = 'object'", name="discovery_oids_is_object"),
        CheckConstraint("jsonb_typeof(neighbor_discovery) = 'object'", name="neighbor_discovery_is_object"),
        CheckConstraint("(status = 'retired') = (retired_at IS NOT NULL)", name="retired_at_matches_status"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    sys_object_id_prefixes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    supported_protocols: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    discovery_oids: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    neighbor_discovery: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class DeviceProfile(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "device_profile"
    __table_args__ = (
        UniqueConstraint("vendor_profile_id", "code", name="uq_device_profile_vendor_code"),
        CheckConstraint(f"status IN {PROFILE_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"device_class IN {DEVICE_CLASSES!r}", name="device_class_allowed"),
        CheckConstraint(f"code ~ '{PROFILE_CODE_PATTERN}'", name="code_format"),
        CheckConstraint("jsonb_typeof(match_criteria) = 'array'", name="match_criteria_is_array"),
        CheckConstraint("jsonb_typeof(capabilities) = 'object'", name="capabilities_is_object"),
        CheckConstraint("jsonb_typeof(interface_discovery) = 'object'", name="interface_discovery_is_object"),
        CheckConstraint("jsonb_typeof(neighbor_behavior) = 'object'", name="neighbor_behavior_is_object"),
        CheckConstraint("(status = 'retired') = (retired_at IS NOT NULL)", name="retired_at_matches_status"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_device_profile_vendor_status", "vendor_profile_id", "status"),
    )

    vendor_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vendor_profile.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    device_class: Mapped[str] = mapped_column(String(16), nullable=False, default="generic", server_default="generic")
    # Declarative AND-list of {field, op, value}; see profile_schema.MatchCriterion.
    match_criteria: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    firmware_min: Mapped[str | None] = mapped_column(String(64))
    firmware_max: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    interface_discovery: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    neighbor_behavior: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ProfileMetricMapping(Base, UUIDPkMixin, TimestampMixin):
    """OID -> canonical metric definition owned by exactly one vendor or device profile.

    A device-profile row overrides a vendor row that maps the same `canonical_metric`;
    see `profile_service.effective_metric_mappings`. `canonical_metric`/`unit` are
    validated against the versioned metric registry (`app/domain/telemetry/registry.py`)
    in the application layer so registry growth needs no migration."""

    __tablename__ = "profile_metric_mapping"
    __table_args__ = (
        CheckConstraint(
            "(vendor_profile_id IS NOT NULL AND device_profile_id IS NULL) OR "
            "(vendor_profile_id IS NULL AND device_profile_id IS NOT NULL)",
            name="single_owner",
        ),
        CheckConstraint(f"value_type IN {METRIC_VALUE_TYPES!r}", name="value_type_allowed"),
        CheckConstraint("scale <> 0", name="scale_nonzero"),
        Index(
            "uq_profile_metric_mapping_vendor_oid", "vendor_profile_id", "oid", unique=True,
            postgresql_where="vendor_profile_id IS NOT NULL",
        ),
        Index(
            "uq_profile_metric_mapping_device_oid", "device_profile_id", "oid", unique=True,
            postgresql_where="device_profile_id IS NOT NULL",
        ),
        Index(
            "uq_profile_metric_mapping_vendor_metric", "vendor_profile_id", "canonical_metric", unique=True,
            postgresql_where="vendor_profile_id IS NOT NULL",
        ),
        Index(
            "uq_profile_metric_mapping_device_metric", "device_profile_id", "canonical_metric", unique=True,
            postgresql_where="device_profile_id IS NOT NULL",
        ),
    )

    vendor_profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendor_profile.id", ondelete="CASCADE"))
    device_profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("device_profile.id", ondelete="CASCADE"))
    oid: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    scale: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=1, server_default="1")
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, default="gauge", server_default="gauge")
    description: Mapped[str | None] = mapped_column(String(255))
