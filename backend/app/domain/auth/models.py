"""User/Role/Permission per ARCHITECTURE_REVIEW.md v1.0 §6.1 (git show 4a39e1b) plus
v1.1's RoleAssignment generalization (replacing UserRole, adding scope_type/scope_id,
§32/§32a). H7 = Option B (recorded): scope_type exists and defaults to 'global', but no
query in this codebase filters by site — enforcement is deliberately not built in Phase 1.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin

SCOPE_TYPES = ("global", "site", "building")


class User(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    role_assignments: Mapped[list["RoleAssignment"]] = relationship(back_populates="user")


class Role(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "role"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role")


class Permission(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "permission"
    __table_args__ = (UniqueConstraint("resource", "action", name="uq_permission_resource_action"),)

    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))

    @property
    def code(self) -> str:
        return f"{self.resource}:{self.action}"


class RolePermission(Base):
    __tablename__ = "role_permission"

    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permission.id", ondelete="CASCADE"), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship()


class RoleAssignment(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "role_assignment"
    __table_args__ = (
        CheckConstraint(f"scope_type IN {SCOPE_TYPES!r}", name="scope_type_allowed"),
        UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", name="uq_role_assignment"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    scope_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="role_assignments")
    role: Mapped[Role] = relationship()


class RefreshToken(Base, UUIDPkMixin, TimestampMixin):
    """Server-side record of issued refresh tokens, keyed by JWT jti, so a token can be
    revoked (logout, rotation) without waiting for its own expiry (§31 Security Hardening:
    "refresh token... rotating... stored server-side hashed for revocation")."""

    __tablename__ = "refresh_token"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True)
    jti: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
