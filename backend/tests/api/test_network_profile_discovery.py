"""Discovery ingestion records an advisory profile-matcher outcome and never binds anything."""

from tests.api._network_helpers import (
    collector_with_integration,
    create_device,
    create_vendor,
    device_record,
    ingest_records,
)


async def test_ingested_facts_record_advisory_match_without_binding(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"], match_criteria=[{"field": "model", "op": "prefix", "value": "C93"}])
    collector, integration = await collector_with_integration(client, headers)

    facts = {"sys_object_id": "1.3.6.1.4.1.9.1.1", "model": "C9300-48P", "firmware": "17.3.1"}
    await ingest_records(client, collector, [device_record(integration["id"], "10.0.0.5", facts=facts)])
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    match = devices[0]["profile_match"]
    assert match["state"] == "matched" and match["device_profile_id"] == device["id"]
    assert match["facts"]["model"] == "C9300-48P"

    unchanged = (await client.get(f"/api/v1/integrations/{integration['id']}", headers=headers)).json()
    assert unchanged["device_profile_id"] is None  # advisory only


async def test_ingest_without_or_with_junk_facts_never_fails(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    records = [
        device_record(integration["id"], "10.0.0.6"),
        device_record(integration["id"], "10.0.0.7", facts="not-a-dict"),
        device_record(integration["id"], "10.0.0.8", facts={"sys_object_id": 5, "model": "x" * 600}),
    ]
    out = await ingest_records(client, collector, records)
    assert [r["status"] for r in out["results"]] == ["accepted"] * 3
    devices = {d["external_identifier"]: d for d in (await client.get("/api/v1/discovery/devices", headers=headers)).json()}
    assert all(d["profile_match"] is None for d in devices.values())


async def test_ambiguous_facts_select_no_profile(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    await create_device(client, headers, vendor["id"], code="dev-a", match_criteria=[{"field": "model", "op": "prefix", "value": "C93"}])
    await create_device(client, headers, vendor["id"], code="dev-b", match_criteria=[{"field": "model", "op": "contains", "value": "300"}])
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [
        device_record(integration["id"], "10.0.1.1", facts={"sys_object_id": "1.3.6.1.4.1.9.1", "model": "C9300"})
    ])
    match = (await client.get("/api/v1/discovery/devices", headers=headers)).json()[0]["profile_match"]
    assert match["state"] == "ambiguous" and match["device_profile_id"] is None
    assert len(match["candidate_device_profile_ids"]) == 2
