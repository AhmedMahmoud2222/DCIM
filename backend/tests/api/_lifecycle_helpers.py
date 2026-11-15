"""Shared helpers and fixtures for Equipment Lifecycle Management API tests."""

import json

from sqlalchemy import text

from app.domain.identity.models import default_lifecycle_states_config


async def reset_lifecycle_settings_row(db_engine) -> None:
    """Reset the singleton lifecycle_settings row to defaults at version 1."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO lifecycle_settings (id, states_config, version) "
                "VALUES (1, CAST(:cfg AS jsonb), 1) "
                "ON CONFLICT (id) DO UPDATE SET states_config = CAST(:cfg AS jsonb), version = 1"
            ),
            {"cfg": json.dumps(default_lifecycle_states_config())},
        )


async def current_lifecycle_version(client, headers) -> int:
    """Return the current optimistic-concurrency version of lifecycle settings."""
    return (await client.get("/api/v1/settings/lifecycle", headers=headers)).json()["version"]


async def put_lifecycle_states(client, headers, states):
    """PUT lifecycle settings using the current version in If-Match."""
    version = await current_lifecycle_version(client, headers)
    return await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states},
        headers={**headers, "If-Match": str(version)},
    )


async def restore_lifecycle_defaults(client, headers):
    """POST lifecycle restore-defaults using the current version in If-Match."""
    version = await current_lifecycle_version(client, headers)
    return await client.post(
        "/api/v1/settings/lifecycle/restore-defaults",
        headers={**headers, "If-Match": str(version)},
    )
