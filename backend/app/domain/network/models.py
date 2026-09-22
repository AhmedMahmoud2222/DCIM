"""Network inventory anchored to ManagedAsset and joined by physical interface FKs."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

DEVICE_TYPES = ("core_switch", "distribution_switch", "access_switch", "router", "firewall", "appliance", "endpoint")
INTERFACE_TYPES = ("physical", "logical", "management", "port_channel")
INTERFACE_ROLES = ("management", "data", "uplink", "server", "unknown")
STATUSES = ("up", "down", "testing", "unknown")
SOURCES = ("operator", "import", "collector", "demo")


class NetworkDevice(Base, TimestampMixin):
    __tablename__ = "network_device"
    __table_args__ = (
        CheckConstraint(f"device_type IN {DEVICE_TYPES!r}", name="device_type_allowed"),
        CheckConstraint(f"source IN {SOURCES!r}", name="source_allowed"),
    )
    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    last_observed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class NetworkInterface(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "network_interface"
    __table_args__ = (
        UniqueConstraint("device_id", "name", name="uq_network_interface_device_name"),
        CheckConstraint(f"interface_type IN {INTERFACE_TYPES!r}", name="interface_type_allowed"),
        CheckConstraint(f"role IN {INTERFACE_ROLES!r}", name="role_allowed"),
        CheckConstraint(f"admin_status IN {STATUSES!r}", name="admin_status_allowed"),
        CheckConstraint(f"oper_status IN {STATUSES!r}", name="oper_status_allowed"),
        CheckConstraint("speed_mbps IS NULL OR speed_mbps > 0", name="speed_positive"),
        CheckConstraint("mtu IS NULL OR mtu > 0", name="mtu_positive"),
        CheckConstraint("native_vlan IS NULL OR native_vlan BETWEEN 1 AND 4094", name="native_vlan_range"),
        CheckConstraint(f"source IN {SOURCES!r}", name="source_allowed"),
    )
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("network_device.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    interface_type: Mapped[str] = mapped_column(String(24), nullable=False, default="physical")
    description: Mapped[str | None] = mapped_column(String(512))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    admin_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    oper_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    speed_mbps: Mapped[int | None] = mapped_column()
    duplex: Mapped[str | None] = mapped_column(String(16))
    mtu: Mapped[int | None] = mapped_column()
    native_vlan: Mapped[int | None] = mapped_column()
    ip_address: Mapped[str | None] = mapped_column(INET)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    last_observed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class NetworkConnection(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "network_connection"
    __table_args__ = (
        CheckConstraint("interface_a_id <> interface_b_id", name="no_self_connection"),
        CheckConstraint(f"source IN {SOURCES!r}", name="source_allowed"),
        UniqueConstraint("interface_a_id", name="uq_network_connection_interface_a"),
        UniqueConstraint("interface_b_id", name="uq_network_connection_interface_b"),
        Index("ix_network_connection_endpoints", "interface_a_id", "interface_b_id"),
    )
    interface_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("network_interface.id", ondelete="RESTRICT"), nullable=False)
    interface_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("network_interface.id", ondelete="RESTRICT"), nullable=False)
    cable_label: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    is_authoritative: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
