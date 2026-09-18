"""Pre-MVP consolidation hardening: end-to-end reproduction/regression tests for
Codex H1 (REST outbound target / SSRF), reassessed against the CONSOLIDATED code
(PRE_MVP_CONSOLIDATION_REPORT.md). Confirms the full path -- API-created REST
integration -> assignment -> `poll-now` -> `run_polling_cycle` ->
`RESTDriver.connect` -> `network_policy.validate_target` -- actually rejects an
unsafe target end-to-end, not only the driver in isolation
(`tests/unit/test_network_policy.py` already covers the policy module itself). Only
safe, controlled local targets are used; no real cloud metadata service or external
host is ever contacted."""

import asyncio

from tests.api._phase8_helpers import create_integration, register_collector


async def test_h1_rest_integration_targeting_metadata_style_address_rejected_end_to_end(client, auth_headers):
    """With NO `rest_integration_allowed_networks` configured (the safe, deny-by-
    default production default this test environment also uses), a REST integration
    pointed at a cloud-metadata-style address must be cleanly rejected by `poll-now`
    -- the central process must never actually attempt this connection."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["rest"]}, headers=headers)
    integration = await create_integration(
        client, headers, integration_type="rest", target_host="169.254.169.254",
        config={"scheme": "http", "path": "/latest/meta-data/"},
    )
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    assert resp.status_code == 200
    outcomes = {o["integration_id"]: o for o in resp.json()}
    outcome = outcomes[integration["id"]]
    assert outcome["succeeded"] is False
    assert "network policy" in (outcome["error"] or "").lower()
    assert "always denied" in (outcome["error"] or "").lower()

    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    assert not any(d["external_identifier"] == "169.254.169.254" for d in devices)


async def test_h1_rest_integration_targeting_rfc1918_address_outside_allowlist_rejected_end_to_end(client, auth_headers):
    """A private RFC1918 address is NOT always-denied (DCIM devices legitimately live
    there) -- but with no allowlist configured for this deployment, it must still be
    rejected, not implicitly trusted just because it looks like a plausible device
    address."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["rest"]}, headers=headers)
    integration = await create_integration(
        client, headers, integration_type="rest", target_host="10.55.0.1", config={"scheme": "http", "path": "/status"},
    )
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    outcomes = {o["integration_id"]: o for o in resp.json()}
    outcome = outcomes[integration["id"]]
    assert outcome["succeeded"] is False
    assert "not within any configured allowed network" in (outcome["error"] or "")


async def test_h1_rest_integration_targeting_operator_allowed_network_succeeds_end_to_end(client, auth_headers, monkeypatch):
    """The positive case: once an operator configures a legitimate device network
    (simulated here by monkeypatching the ONE function that turns central Settings
    into a `NetworkPolicy` -- exactly the seam `collector_service._build_rest_network_policy`
    exists for), a REST integration whose target resolves inside that allowlist is
    genuinely polled -- proving this hardening does not just deny everything, it
    implements the "DCIM must legitimately reach private devices" requirement too."""
    from app.application.drivers.network_policy import NetworkPolicy

    async def _handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{"status":"ok"}')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        monkeypatch.setattr(
            "app.application.collector_service._build_rest_network_policy",
            lambda: NetworkPolicy(allow_loopback=True, allowed_ports=frozenset({port})),
        )
        headers = await auth_headers("DCIM Manager")
        collector = await register_collector(client, headers)
        await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["rest"]}, headers=headers)
        integration = await create_integration(
            client, headers, integration_type="rest", target_host="127.0.0.1", target_port=port,
            config={"scheme": "http", "path": "/status"},
        )
        await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

        resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
        outcomes = {o["integration_id"]: o for o in resp.json()}
        assert outcomes[integration["id"]]["succeeded"] is True
    finally:
        server.close()
        await server.wait_closed()
