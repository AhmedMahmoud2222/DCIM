"""Shared fixture-building helpers for Phase 8 tests -- mirrors tests/api/_phase3_helpers.py's style."""

import time
import uuid

from app.application.collector_auth import compute_signature


def sign_request(*, secret: str, collector_id: uuid.UUID, raw_body: bytes, nonce: str | None = None, timestamp: str | None = None) -> dict:
    """Builds the four X-Collector-* headers a real collector would send."""
    ts = timestamp or str(int(time.time()))
    n = nonce or uuid.uuid4().hex
    sig = compute_signature(secret=secret, collector_id=collector_id, timestamp=ts, nonce=n, raw_body=raw_body)
    return {
        "X-Collector-Id": str(collector_id),
        "X-Collector-Timestamp": ts,
        "X-Collector-Nonce": n,
        "X-Collector-Signature": sig,
    }


async def register_collector(client, headers, *, name=None, collector_type="central", site_id=None) -> dict:
    resp = await client.post(
        "/api/v1/collectors",
        json={"name": name or f"collector-{uuid.uuid4().hex[:8]}", "collector_type": collector_type, "site_id": str(site_id) if site_id else None},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_integration(client, headers, *, name=None, integration_type="icmp", target_host="127.0.0.1", **extra) -> dict:
    body = {
        "name": name or f"integration-{uuid.uuid4().hex[:8]}", "integration_type": integration_type,
        "target_host": target_host,
    }
    body.update(extra)
    resp = await client.post("/api/v1/integrations", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()
