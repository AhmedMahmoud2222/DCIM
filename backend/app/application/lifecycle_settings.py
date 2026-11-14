"""Application service and validation for administrator-configurable equipment lifecycle settings."""

import copy
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity.models import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    LEGACY_LIFECYCLE_STATUSES,
    LIFECYCLE_BADGE_COLORS,
    MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS,
    VISIBLE_LIFECYCLE_STATUSES,
    LifecycleSettings,
    default_lifecycle_states_config,
)


class LifecycleStateConfigItem(BaseModel):
    status: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    badge_color: str = Field(min_length=1, max_length=32)
    display_order: int = Field(ge=1, le=6)
    allowed_transitions: list[str] = Field(default_factory=list)


class LifecycleSettingsUpdate(BaseModel):
    states: list[LifecycleStateConfigItem]


class LifecycleSettingsRead(BaseModel):
    states: list[LifecycleStateConfigItem]
    mandatory_allowed_transitions: dict[str, list[str]]
    legacy_states: list[str]
    badge_colors: list[str]


def validate_lifecycle_states_update(states: list[LifecycleStateConfigItem]) -> list[dict[str, Any]]:
    """Validate administrator lifecycle settings against domain invariants and mandatory restrictions."""
    seen_statuses: list[str] = []
    for item in states:
        if item.status in LEGACY_LIFECYCLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Legacy status '{item.status}' cannot be configured or exposed as a selectable state.",
            )
        seen_statuses.append(item.status)

    if len(seen_statuses) != len(VISIBLE_LIFECYCLE_STATUSES) or set(seen_statuses) != set(VISIBLE_LIFECYCLE_STATUSES):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Lifecycle settings must include exactly the six visible states without additions, deletions, "
                f"or duplicates: {', '.join(VISIBLE_LIFECYCLE_STATUSES)}."
            ),
        )

    orders = [item.display_order for item in states]
    if set(orders) != set(range(1, len(VISIBLE_LIFECYCLE_STATUSES) + 1)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Each of the six states must have a unique display_order from 1 to 6.",
        )

    normalized: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in states:
        clean_label = item.label.strip()
        if not clean_label:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Display label for state '{item.status}' cannot be blank.",
            )
        lower_label = clean_label.lower()
        if lower_label in seen_labels:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Duplicate display label '{clean_label}' is not allowed.",
            )
        seen_labels.add(lower_label)

        if item.badge_color not in LIFECYCLE_BADGE_COLORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Invalid badge_color '{item.badge_color}' for state '{item.status}'. "
                    f"Allowed values: {', '.join(LIFECYCLE_BADGE_COLORS)}."
                ),
            )

        mandatory_allowed = set(MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS[item.status])
        deduped_targets: list[str] = []
        for target in item.allowed_transitions:
            if target in LEGACY_LIFECYCLE_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Legacy status '{target}' cannot be exposed as a selectable target state.",
                )
            if target not in VISIBLE_LIFECYCLE_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Unknown target lifecycle status '{target}'.",
                )
            if target == item.status:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Self-transition from '{item.status}' to '{target}' is not allowed.",
                )
            if target not in mandatory_allowed:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Transition from '{item.status}' to '{target}' is forbidden by mandatory backend "
                        f"restrictions (permitted targets for '{item.status}': "
                        f"{sorted(mandatory_allowed) or 'none'})."
                    ),
                )
            if target not in deduped_targets:
                deduped_targets.append(target)

        normalized.append(
            {
                "status": item.status,
                "label": clean_label,
                "description": item.description.strip(),
                "badge_color": item.badge_color,
                "display_order": item.display_order,
                "allowed_transitions": deduped_targets,
            }
        )

    normalized.sort(key=lambda entry: entry["display_order"])
    return normalized


async def get_effective_lifecycle_states(db: AsyncSession) -> list[dict[str, Any]]:
    row = (await db.execute(select(LifecycleSettings).where(LifecycleSettings.id == 1))).scalar_one_or_none()
    if row is None or not isinstance(row.states_config, list) or len(row.states_config) != len(VISIBLE_LIFECYCLE_STATUSES):
        return default_lifecycle_states_config()
    states = copy.deepcopy(row.states_config)
    states.sort(key=lambda entry: int(entry.get("display_order", 99)))
    return states


def build_lifecycle_settings_read(states: list[dict[str, Any]]) -> LifecycleSettingsRead:
    return LifecycleSettingsRead(
        states=[LifecycleStateConfigItem(**item) for item in states],
        mandatory_allowed_transitions=copy.deepcopy(MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS),
        legacy_states=list(LEGACY_LIFECYCLE_STATUSES),
        badge_colors=list(LIFECYCLE_BADGE_COLORS),
    )


async def get_configured_allowed_transitions(db: AsyncSession, from_status: str) -> set[str]:
    """Return allowed target states for `from_status`, intersected with mandatory backend domain rules."""
    domain_allowed = set(ALLOWED_LIFECYCLE_TRANSITIONS.get(from_status, set())) - set(LEGACY_LIFECYCLE_STATUSES)
    if from_status not in VISIBLE_LIFECYCLE_STATUSES:
        return domain_allowed
    states = await get_effective_lifecycle_states(db)
    for item in states:
        if item.get("status") == from_status:
            configured = set(item.get("allowed_transitions") or [])
            return domain_allowed & configured
    return domain_allowed
