"""Pre-MVP consolidation hardening: FV2 -- collector capability declaration and
integration config now have explicit, documented size/nesting bounds (previously
unbounded, a real gap `PHASE8_FINAL_CLOSURE_VALIDATION.md` discovered and
`test_phase8_final_closure_validation.py::test_security_sweep_capability_declaration_has_no_payload_size_bound`
documented as accepted-with-no-bound). That earlier test's own docstring says to
update it once a bound exists -- this file is that update, plus the new config bound."""

import uuid

from tests.api._phase8_helpers import create_integration, register_collector


async def test_capability_list_over_the_new_bound_is_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    too_many = [f"code-{i}" for i in range(65)]
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": too_many}, headers=headers)
    assert resp.status_code == 422, resp.text


async def test_capability_list_at_the_bound_is_accepted(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    exactly_64 = [f"code-{i}" for i in range(64)]
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": exactly_64}, headers=headers)
    assert resp.status_code == 204, resp.text


async def test_overlong_single_capability_code_is_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["x" * 65]}, headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_oversized_integration_config_rejected_on_create(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    resp = await client.post(
        "/api/v1/integrations",
        json={
            "name": f"cfg-{uuid.uuid4().hex[:8]}", "integration_type": "rest", "target_host": "10.10.0.1",
            "config": {"blob": "x" * 20_000},
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_reasonable_integration_config_still_accepted(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(
        client, headers, integration_type="rest",
        config={"scheme": "https", "path": "/status", "method": "GET", "headers": {"Accept": "application/json"}},
    )
    assert integration["id"]


async def test_deeply_nested_integration_config_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    nested: dict = {"v": 1}
    for _ in range(10):
        nested = {"nested": nested}
    resp = await client.post(
        "/api/v1/integrations",
        json={"name": f"deep-{uuid.uuid4().hex[:8]}", "integration_type": "rest", "target_host": "10.10.0.1", "config": nested},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_oversized_config_rejected_on_patch(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers, integration_type="rest")
    resp = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"config": {"blob": "x" * 20_000}},
        headers={**headers, "If-Match": str(integration["version"])},
    )
    assert resp.status_code == 422, resp.text
