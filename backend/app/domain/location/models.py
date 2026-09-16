"""Location hierarchy: Organization -> Country -> City -> Site -> Building -> Floor -> Room.

Schema per ARCHITECTURE_REVIEW.md v1.0 §6.2 (git show 4a39e1b), reaffirmed by AD1 (§46,
unchanged through v1.1-v1.3) but never re-published after v1.1's rewrite — see
PHASE1_BASELINE.md for why this is treated as a documentation gap, not a contradiction,
and PHASE1_DEVIATIONS.md item D1 for the recommended follow-up.
"""

import uuid

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class LocationType(Base, UUIDPkMixin):
    """Metadata registry driving frontend breadcrumb/tree navigation generically
    (ARCHITECTURE_REVIEW.md AD1) — not a polymorphic replacement for the tables below."""

    __tablename__ = "location_type"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)


class Organization(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    countries: Mapped[list["Country"]] = relationship(back_populates="organization")


class Country(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "country"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    iso_code: Mapped[str | None] = mapped_column(String(2))

    organization: Mapped[Organization] = relationship(back_populates="countries")
    cities: Mapped[list["City"]] = relationship(back_populates="country")


class City(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "city"

    country_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("country.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    country: Mapped[Country] = relationship(back_populates="cities")
    sites: Mapped[list["Site"]] = relationship(back_populates="city")


class Site(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "site"

    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("city.id", ondelete="RESTRICT"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    city: Mapped[City] = relationship(back_populates="sites")
    buildings: Mapped[list["Building"]] = relationship(back_populates="site")


class Building(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "building"
    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_building_site_code"),)

    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    site: Mapped[Site] = relationship(back_populates="buildings")
    floors: Mapped[list["Floor"]] = relationship(back_populates="building")


class Floor(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "floor"
    __table_args__ = (UniqueConstraint("building_id", "level_number", name="uq_floor_building_level"),)

    building_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("building.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    level_number: Mapped[int] = mapped_column(nullable=False)

    building: Mapped[Building] = relationship(back_populates="floors")
    rooms: Mapped[list["Room"]] = relationship(back_populates="floor")


class Room(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "room"
    __table_args__ = (UniqueConstraint("floor_id", "code", name="uq_room_floor_code"),)

    floor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("floor.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    room_type: Mapped[str] = mapped_column(String(32), nullable=False, default="data_hall")
    area_sqm: Mapped[float | None] = mapped_column(Numeric(10, 2))
    raised_floor: Mapped[bool] = mapped_column(nullable=False, default=False)

    version: Mapped[int] = mapped_column(nullable=False, default=1)
    """Phase 1 concurrency-foundation demonstration entity (ARCHITECTURE_REVIEW.md §17 of
    the Phase 1 prompt: "do not build future domain models merely to demonstrate it" —
    Room already exists as a real Phase 1 entity, so it carries the reusable version/
    If-Match pattern that RackPlacement/EquipmentPlacement/PowerConnection will use later)."""

    floor: Mapped[Floor] = relationship(back_populates="rooms")
