# ruff: noqa: UP031  (JSON literals are written with %-formatting so the tests control the exact numeral bytes)
"""Issue #128 / G3 end to end: real HTTP, real signatures, PostgreSQL. Exact source numerals, one rounding, canonical
reproducible from the persisted raw value, additive responses, and legacy rows left alone."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from edge_collector.telemetry_contract import ContractBook, telemetry_record_payload
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.application.mapping_revision_service import append_mapping_revision
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    IntegrationMetricMappingRevision,
    TelemetryContractHold,
    TelemetryReading,
)
from app.domain.telemetry.registry import convert_to_canonical
from tests.api._network_helpers import collector_with_integration, signed_get
from tests.api._phase8_helpers import sign_request

D = Decimal


@pytest.fixture
async def world(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    ids = {}
    for source, metric, unit, scale in (
        ("kw", "power_kw", "kW", "1"), ("w", "power_kw", "W", "1"), ("f", "temperature_c", "degF", "1"),
        ("ratio", "load_percent", "1", "1"), ("scaled", "power_kw", "kW", "10"), ("avail", "availability", "1", "1"),
    ):
        response = await client.post(
            "/api/v1/telemetry/mappings",
            content=('{"integration_id":"%s","source_identifier":"%s","canonical_metric":"%s","unit":"%s","scale":%s}'
                     % (integration["id"], source, metric, unit, scale)).encode(),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 201, response.text
        ids[source] = response.json()
    return {"headers": headers, "collector": collector, "integration": integration, "mappings": ids}


def occurred() -> str:
    return (datetime.now(UTC) + timedelta(seconds=1)).isoformat()


def rec(world, source, literal, key=None):
    """A raw JSON record with the value written exactly as `literal` (so the test controls the numeral)."""
    return ('{"dedup_key":"%s","integration_id":"%s","external_identifier":"e","source_identifier":"%s",'
            '"occurred_at":"%s","mapping_revision_id":"%s","value":%s}'
            % (key or uuid.uuid4().hex, world["integration"]["id"], source, occurred(),
               world["mappings"][source]["current_revision_id"], literal))


async def post_raw(client, world, body: bytes, *, expect=200, collector=None):
    collector = collector or world["collector"]
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=body)
    headers["Content-Type"] = "application/json"
    response = await client.post(f"/api/v1/collectors/{collector['id']}/telemetry", content=body, headers=headers)
    assert response.status_code == expect, response.text
    return response


async def send(client, world, source, literal, key=None):
    key = key or uuid.uuid4().hex
    body = ('{"records":[%s]}' % rec(world, source, literal, key)).encode()
    (ack,) = (await post_raw(client, world, body)).json()["results"]
    return ack, key


async def stored(db_session, key):
    row = (await db_session.execute(select(TelemetryReading).where(TelemetryReading.dedup_key == key))).scalar_one()
    await db_session.refresh(row)
    return row


@pytest.mark.parametrize("literal,raw,canonical", [
    ("1234567890.12345678", "1234567890.12345678", "1234567890.12345678"),   # not representable as a float
    ("9999999999.99999999", "9999999999.99999999", "9999999999.99999999"),   # the documented maximum, over HTTP
    ("-9999999999.99999999", "-9999999999.99999999", "-9999999999.99999999"),
    ("0.1", "0.10000000", "0.10000000"),
    ("1.000000005", "1.00000000", "1.00000000"),                              # tie, even neighbour -> down
    ("1.000000015", "1.00000002", "1.00000002"),                              # tie, odd neighbour  -> up
    ("2.000000025", "2.00000002", "2.00000002"),
    ("-1.000000005", "-1.00000000", "-1.00000000"),
    ("-2.000000025", "-2.00000002", "-2.00000002"),
    ("1.0000000050001", "1.00000001", "1.00000001"),
    ('"1234567890.12345678"', "1234567890.12345678", "1234567890.12345678"),   # decimal string
    ("12345", "12345.00000000", "12345.00000000"),                            # JSON integer
    ("1e-9", "0.00000000", "0.00000000"),
    ("2.5E+3", "2500.00000000", "2500.00000000"),
])
async def test_exact_numeral_one_rounding_and_canonical_reproducible_from_stored_raw(client, world, db_session, literal, raw, canonical):
    ack, key = await send(client, world, "kw", literal)
    assert (ack["status"], ack["error"]) == ("accepted", None)
    row = await stored(db_session, key)
    assert (row.raw_value, row.value) == (D(raw), D(canonical))
    assert row.raw_value_text == literal.strip('"')
    # the G3 invariant, recomputed from persisted evidence only
    assert convert_to_canonical(row.metric, D(str(row.raw_value)), row.raw_unit, source_scale=D(str(row.source_scale))).value == row.value


@pytest.mark.parametrize("source,literal,canonical", [
    ("w", "1000.5", "1.00050000"),
    ("w", "1000000.000000005", "1000.00000000"),     # raw rounds first (half-even -> ...000), then /1000
    ("f", "68.000000005", "20.00000000"),
    ("f", "68.000000015", "20.00000001"),
    ("scaled", "1.000000005", "10.00000000"),
    ("ratio", "0.123456785", "12.34567800"),
])
async def test_conversion_and_scale_start_from_the_stored_raw(client, world, db_session, source, literal, canonical):
    ack, key = await send(client, world, source, literal)
    assert ack["status"] == "accepted", ack
    row = await stored(db_session, key)
    assert row.value == D(canonical)
    assert convert_to_canonical(row.metric, D(str(row.raw_value)), row.raw_unit, source_scale=D(str(row.source_scale))).value == row.value


@pytest.mark.parametrize("literal", [
    "9999999999.999999994",   # rounds down into range; the original is above the documented maximum
    "9999999999.999999995", "10000000000", "-10000000000", "1e10", "1E+400", "1e-101", "NaN", "Infinity", "-Infinity",
    '"NaN"', '"Infinity"', '"-inf"',
])
async def test_unstorable_numbers_are_rejected_per_record_and_peers_persist(client, world, db_session, literal):
    body = ('{"records":[%s,%s,%s]}' % (rec(world, "kw", "1.5", "a-good"), rec(world, "kw", literal, "bad"), rec(world, "kw", "2.5", "z-good"))).encode()
    results = (await post_raw(client, world, body)).json()["results"]
    assert [(r["dedup_key"], r["status"], r["error"]) for r in results] == [
        ("a-good", "accepted", None), ("bad", "rejected", "INVALID_TELEMETRY_VALUE"), ("z-good", "accepted", None)]
    assert sorted((await db_session.execute(select(TelemetryReading.dedup_key))).scalars().all()) == ["a-good", "z-good"]
    # a rejected record is rejected again on retry, never reported as a duplicate
    again = (await post_raw(client, world, ('{"records":[%s]}' % rec(world, "kw", literal, "bad")).encode())).json()["results"]
    assert again[0]["error"] == "INVALID_TELEMETRY_VALUE"


@pytest.mark.parametrize("source,literal", [
    ("scaled", "1000000000.5"),        # raw fits, raw * scale (10) overflows
    ("scaled", "1000000000"),          # exactly 1e10 after scaling
    ("scaled", "999999999.999999999"), # raw rounds UP to 1e9 first, then scales to 1e10
    ("ratio", "1000000000"),           # raw and scaled fit, canonical (x100 to percent) overflows
    ("ratio", "99999999.999999999"),   # canonical just over
])
async def test_scaled_and_canonical_overflow_are_rejected_explicitly(client, world, db_session, source, literal):
    ack, _ = await send(client, world, source, literal)
    assert (ack["status"], ack["error"]) == ("rejected", "INVALID_TELEMETRY_VALUE")
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


@pytest.mark.parametrize("literal", ["-0", "-0.0", "-0e0", "0"])
async def test_signed_zero_keeps_its_text_and_stores_zero(client, world, db_session, literal):
    ack, key = await send(client, world, "kw", literal)
    assert ack["status"] == "accepted"
    row = await stored(db_session, key)
    assert row.raw_value == 0 and row.value == 0 and row.raw_value_text == literal
    latest = (await client.get("/api/v1/telemetry/latest", headers=world["headers"], params={"integration_id": world["integration"]["id"]})).json()
    assert latest[0]["raw_value_text"] == literal and latest[0]["raw_value_decimal"] == "0.00000000"


@pytest.mark.parametrize("literal", ["true", "false", "null", '"abc"', '""', '"0x1"', "[1]", "{}", '"' + "1" * 65 + '"', "1." + "5" * 64])
async def test_structurally_malformed_values_are_a_422_for_the_whole_request(client, world, db_session, literal):
    body = ('{"records":[%s,%s]}' % (rec(world, "kw", "1", "ok"), rec(world, "kw", literal, "bad"))).encode()
    response = await post_raw(client, world, body, expect=422)
    assert response.json()["title"] == "Validation Error"
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


@pytest.mark.parametrize("body", [b"", b"{", b'{"records":', b'{"records":[{"value":1,]}', b"[]", b'{"records":"x"}',
                                  b'{"records":[1]}', b"{\"records\":[{\"value\":1}]}\x00"])
async def test_malformed_json_is_a_422_not_a_500(client, world, body):
    await post_raw(client, world, body, expect=422)


async def test_authentication_batch_limit_and_size_cap_are_unchanged(client, world, db_session):
    body = ('{"records":[%s]}' % rec(world, "kw", "1")).encode()
    forged = dict(world["collector"], secret="wrong-secret-wrong-secret-wrong-secret")
    headers = sign_request(secret=forged["secret"], collector_id=uuid.UUID(forged["id"]), raw_body=body)
    headers["Content-Type"] = "application/json"
    bad = await client.post(f"/api/v1/collectors/{forged['id']}/telemetry", content=body, headers=headers)
    assert bad.status_code == 401
    unsigned = await client.post(f"/api/v1/collectors/{forged['id']}/telemetry", content=body)
    assert unsigned.status_code in {401, 422}
    many = ('{"records":[%s]}' % ",".join(rec(world, "kw", "1") for _ in range(501))).encode()
    await post_raw(client, world, many, expect=422)
    huge = b'{"records":[],"pad":"' + b"x" * (8 * 1024 * 1024) + b'"}'
    await post_raw(client, world, huge, expect=413)
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


async def test_attributes_keep_their_json_numbers(client, world, db_session):
    body = ('{"records":[{"dedup_key":"attr","integration_id":"%s","external_identifier":"e","source_identifier":"kw",'
            '"occurred_at":"%s","mapping_revision_id":"%s","value":1.5,"attributes":{"n":3,"f":0.1,"deep":[1.25,{"i":7}]}}]}'
            % (world["integration"]["id"], occurred(), world["mappings"]["kw"]["current_revision_id"])).encode()
    (ack,) = (await post_raw(client, world, body)).json()["results"]
    assert ack["status"] == "accepted"
    assert (await stored(db_session, "attr")).attributes == {"n": 3, "f": 0.1, "deep": [1.25, {"i": 7}]}


async def test_replay_and_dedup_keep_the_first_exact_evidence(client, world, db_session):
    ack, key = await send(client, world, "kw", "1.000000005", key="same")
    assert ack["status"] == "accepted"
    again, _ = await send(client, world, "kw", "9.99999999", key="same")
    assert again["status"] == "duplicate"
    row = await stored(db_session, "same")
    assert (row.raw_value_text, row.raw_value) == ("1.000000005", D("1.00000000"))


async def test_g1_pinning_still_selects_the_conversion_and_the_numeral_is_exact(client, world, db_session):
    mapping_id = uuid.UUID(world["mappings"]["f"]["id"])
    first = world["mappings"]["f"]["current_revision_id"]
    await append_mapping_revision(db_session, mapping_id, unit="degC")
    await db_session.commit()
    ack, key = await send(client, world, "f", "68.000000005")   # pinned to revision 1 (degF) by the helper
    row = await stored(db_session, key)
    assert ack["status"] == "accepted" and str(row.mapping_revision_id) == first
    assert (row.raw_unit, row.value, row.raw_value_text) == ("degF", D("20.00000000"), "68.000000005")


async def test_hold_round_trip_keeps_a_long_exact_numeral_and_the_operator_release_stores_it_exactly(client, world, db_session):
    mapping_id = uuid.UUID(world["mappings"]["kw"]["id"])
    first = world["mappings"]["kw"]["current_revision_id"]
    await append_mapping_revision(db_session, mapping_id, unit="W")
    await db_session.commit()
    body = ('{"records":[{"dedup_key":"held","integration_id":"%s","external_identifier":"e","source_identifier":"kw",'
            '"occurred_at":"%s","value":1234567890.12345678}]}' % (world["integration"]["id"], occurred())).encode()
    (ack,) = (await post_raw(client, world, body)).json()["results"]
    assert ack["error"] == "AMBIGUOUS_MAPPING_CONTRACT"
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    assert hold.value_text == "1234567890.12345678"
    resolved = await client.post(f"/api/v1/telemetry/contract-holds/{hold.id}/resolve", json={"mapping_revision_id": first}, headers=world["headers"])
    assert resolved.status_code == 200, resolved.text
    row = await stored(db_session, "held")
    assert (row.raw_value, row.raw_value_text, row.contract_evidence) == (D("1234567890.12345678"), "1234567890.12345678", "operator_resolved")
    # a long (but allowed) numeral round-trips through the hold table too
    long = "0." + "1" * 62
    body = body.replace(b"1234567890.12345678", long.encode()).replace(b'"held"', b'"held2"')
    await post_raw(client, world, body)
    assert (await db_session.execute(select(TelemetryContractHold.value_text).where(TelemetryContractHold.dedup_key == "held2"))).scalar_one() == long


async def test_alarm_evaluation_uses_the_canonical_value_from_the_rounded_raw(client, world, db_session):
    rule = AlarmRule(id=uuid.uuid4(), integration_id=uuid.UUID(world["integration"]["id"]), metric="power_kw",
                     rule_type="threshold_high", threshold=D("1.00000000"), unit="kW", registry_version="1", name="edge")
    db_session.add(rule)
    await db_session.commit()
    await send(client, world, "kw", "1.000000005")     # half-even -> 1.00000000, not above the threshold
    await send(client, world, "kw", "1.000000015")     # -> 1.00000002, above
    alarms = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalars().all()
    assert len(alarms) == 1 and D(str(alarms[0].last_value)) == D("1.00000002")


async def test_response_fields_are_additive_exact_and_deterministic(client, world, db_session):
    await send(client, world, "kw", "1234567890.123456789")
    await send(client, world, "kw", "0.00000001", key="tiny")
    rows = (await client.get("/api/v1/telemetry/latest", headers=world["headers"], params={"integration_id": world["integration"]["id"]})).json()
    by_text = {r["raw_value_text"]: r for r in rows}
    big = by_text["1234567890.123456789"]
    assert (big["value_decimal"], big["raw_value_decimal"], big["source_scale_decimal"]) == ("1234567890.12345679", "1234567890.12345679", "1.00000000")
    assert isinstance(big["value"], float) and isinstance(big["raw_value"], float) and isinstance(big["source_scale"], float)
    tiny = by_text["0.00000001"]
    assert tiny["raw_value_decimal"] == "0.00000001" and tiny["value_decimal"] == "0.00000001"   # never "1E-8"
    history = (await client.get("/api/v1/telemetry/history", headers=world["headers"], params={
        "metric": "power_kw", "integration_id": world["integration"]["id"],
        "start": (datetime.now(UTC) - timedelta(days=1)).isoformat(), "end": (datetime.now(UTC) + timedelta(days=1)).isoformat()})).json()
    assert {p["resolution"] for p in history} == {"raw"} and all(p["raw_value_text"] for p in history)


async def test_legacy_rows_stay_readable_without_invented_precision_and_daily_rows_have_no_numeral(client, world, db_session):
    mapping = (await db_session.execute(select(IntegrationMetricMapping).where(IntegrationMetricMapping.source_identifier == "kw"))).scalar_one()
    now = datetime.now(UTC)
    day = (now - timedelta(days=500)).replace(hour=3)
    db_session.add(TelemetryReading(
        id=uuid.uuid4(), collector_id=uuid.UUID(world["collector"]["id"]), integration_id=mapping.integration_id, mapping_id=mapping.id,
        external_identifier="legacy", series_key="legacy-series", dedup_key="legacy", metric="power_kw", unit="kW", value=D("1.00000001"),
        raw_value=D("1.00000001"), raw_unit="kW", source_scale=D("1"), registry_version="1", occurred_at=now - timedelta(minutes=5),
        received_at=now, attributes={},
    ))
    db_session.add(DailyTelemetryAggregate(
        id=uuid.uuid4(), integration_id=mapping.integration_id, external_identifier="legacy", series_key="daily-series", metric="power_kw", unit="kW",
        day=day.date(), average_value=D("2.50000000"), minimum_value=D("1"), maximum_value=D("4"), sample_count=3, registry_version="1",
    ))
    await db_session.commit()
    rows = (await client.get("/api/v1/telemetry/latest", headers=world["headers"], params={"integration_id": world["integration"]["id"]})).json()
    legacy = rows[0]
    assert (legacy["raw_value_text"], legacy["raw_value_decimal"], legacy["value_decimal"]) == (None, "1.00000001", "1.00000001")
    history = (await client.get("/api/v1/telemetry/history", headers=world["headers"], params={
        "metric": "power_kw", "integration_id": world["integration"]["id"],
        "start": (now - timedelta(days=600)).isoformat(), "end": now.isoformat()})).json()
    daily = next(p for p in history if p["resolution"] == "daily")
    assert (daily["value_decimal"], daily["raw_value_decimal"], daily["raw_value_text"], daily["source_scale_decimal"]) == ("2.50000000", None, None, None)
    assert daily["value"] == 2.5 and daily["minimum_value"] == 1.0   # existing float fields unchanged


async def test_database_constraint_rejects_a_non_numeral_or_overlong_source_text(client, world, db_session):
    _, key = await send(client, world, "kw", "1.5")
    row = await stored(db_session, key)
    for bad in ("abc", "1.2.3", "", "1" * 65, " 1", "NaN", "0x10", "1e", "--1"):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(text("UPDATE telemetry_reading SET raw_value_text = :t WHERE id = :i"), {"t": bad, "i": row.id})
    for good in ("1", "-0", "+.5", "1.", "1E+3", "0." + "1" * 62):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE telemetry_reading SET raw_value_text = :t WHERE id = :i"), {"t": good, "i": row.id})


async def test_collector_helper_preserves_exact_values_and_central_accepts_them(client, world, db_session):
    plan = (await signed_get(client, world["collector"], "telemetry-contracts")).json()
    book = ContractBook.from_plan(plan)
    payload = telemetry_record_payload(book, integration_id=world["integration"]["id"], source_identifier="kw",
                                       external_identifier="e", value=D("1234567890.12345678"))
    assert payload["value"] == "1234567890.12345678"
    wire = {**payload, "dedup_key": "from-helper", "occurred_at": occurred()}
    await post_raw(client, world, json.dumps({"records": [wire]}).encode())
    row = await stored(db_session, "from-helper")
    assert (row.raw_value, row.raw_value_text) == (D("1234567890.12345678"), "1234567890.12345678")


async def test_mapping_scale_is_exact_and_bounded(client, world, db_session):
    headers = {**world["headers"], "Content-Type": "application/json"}

    async def create(source, scale_literal, expect):
        body = ('{"integration_id":"%s","source_identifier":"%s","canonical_metric":"power_kw","unit":"kW","scale":%s}'
                % (world["integration"]["id"], source, scale_literal)).encode()
        response = await client.post("/api/v1/telemetry/mappings", content=body, headers=headers)
        assert response.status_code == expect, (scale_literal, response.text)
        return response.json()

    ok = await create("s1", "1234567890.12345678", 201)
    assert ok["scale_decimal"] == "1234567890.12345678"
    stored_scale = (await db_session.execute(select(IntegrationMetricMapping.scale).where(IntegrationMetricMapping.id == uuid.UUID(ok["id"])))).scalar_one()
    assert stored_scale == D("1234567890.12345678")
    revision = await db_session.get(IntegrationMetricMappingRevision, uuid.UUID(ok["current_revision_id"]))
    assert revision.source_scale == D("1234567890.12345678")
    assert (await create("s2", '"0.1"', 201))["scale_decimal"] == "0.10000000"
    assert (await create("s3", "0.1", 201))["scale"] == 0.1
    for index, literal in enumerate(["0.123456789", "10000000000", '"abc"', "true", '"NaN"', "1e101", "[1]"]):
        await create(f"bad{index}", literal, 422)
    unauthenticated = await client.post("/api/v1/telemetry/mappings", content=b"not json", headers={"Content-Type": "application/json"})
    assert unauthenticated.status_code == 401   # authorisation is decided before the body is read
    listed = (await client.get("/api/v1/telemetry/mappings", headers=world["headers"])).json()
    assert {m["scale_decimal"] for m in listed} >= {"1.00000000", "10.00000000", "1234567890.12345678"}


async def test_openapi_documents_the_mapping_body_and_the_additive_response_fields():
    from app.main import app

    schema = app.openapi()
    body = schema["paths"]["/api/v1/telemetry/mappings"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "scale" in body["properties"]
    out = schema["components"]["schemas"]["TelemetryOut"]["properties"]
    assert {"value_decimal", "raw_value_decimal", "source_scale_decimal", "raw_value_text", "value", "raw_value"} <= set(out)


async def test_legacy_pre_registry_mapping_rounds_the_scaled_value_once_half_even(client, world, db_session):
    from sqlalchemy import null
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=uuid.UUID(world["integration"]["id"]), source_identifier="legacy-src",
                                       canonical_metric="power_kw", unit="widgets", scale=D("0.5"), registry_version=null())
    db_session.add(mapping)
    await db_session.commit()
    await db_session.refresh(mapping)
    body = ('{"records":[{"dedup_key":"lg","integration_id":"%s","external_identifier":"e","source_identifier":"legacy-src",'
            '"occurred_at":"%s","mapping_revision_id":"%s","value":0.000000015}]}' % (world["integration"]["id"], occurred(), mapping.current_revision_id)).encode()
    (ack,) = (await post_raw(client, world, body)).json()["results"]
    assert ack["status"] == "accepted", ack
    row = await stored(db_session, "lg")
    # raw 0.000000015 -> 0.00000002 (half-even); legacy stores raw * scale = 0.00000001 exactly
    assert (row.raw_value, row.value, row.unit, row.registry_version) == (D("0.00000002"), D("0.00000001"), "widgets", None)
