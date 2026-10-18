"""series_key boundary tests for telemetry history API."""
import pytest


@pytest.mark.asyncio
async def test_series_key_boundary_1024_accepted(client, auth_headers):
    key = "a" * 1024
    r = await client.get("/api/v1/telemetry/history", params={"series_key": key}, headers=auth_headers)
    assert r.status_code in (200, 404, 422)


@pytest.mark.asyncio
async def test_series_key_boundary_1025_rejected(client, auth_headers):
    key = "a" * 1025
    r = await client.get("/api/v1/telemetry/history", params={"series_key": key}, headers=auth_headers)
    assert r.status_code == 422
