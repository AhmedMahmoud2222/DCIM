"""series_key boundary tests for telemetry history API."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_series_key_boundary_1024_accepted(client, auth_headers):
    headers = await auth_headers()
    now = datetime.now(UTC)
    key = "a" * 1024
    r = await client.get("/api/v1/telemetry/history", params={
        "metric": "temperature_c",
        "start": (now - timedelta(days=1)).isoformat(),
        "end": now.isoformat(),
        "integration_id": str(uuid.uuid4()),
        "series_key": key,
    }, headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_series_key_boundary_1025_rejected(client, auth_headers):
    headers = await auth_headers()
    now = datetime.now(UTC)
    key = "a" * 1025
    r = await client.get("/api/v1/telemetry/history", params={
        "metric": "temperature_c",
        "start": (now - timedelta(days=1)).isoformat(),
        "end": now.isoformat(),
        "integration_id": str(uuid.uuid4()),
        "series_key": key,
    }, headers=headers)
    assert r.status_code == 422
    assert "series_key" in r.text
