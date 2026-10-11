"""Issue #128 / G4-A over signed HTTP: a late sample for a compacted day is merged exactly, and an exhausted aggregate
refuses the one record without losing it or its peers."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from edge_collector.telemetry_contract import classify_telemetry_ack
from sqlalchemy import select

from app.application.telemetry_retention import compact_eligible_raw
from app.domain.telemetry.aggregate_math import MAX_COUNT
from app.domain.telemetry.models import DailyTelemetryAggregate, TelemetryReading
from tests.api._network_helpers import collector_with_integration
from tests.api._phase8_helpers import sign_request

D = Decimal
OLD_DAY = datetime(2025, 8, 15, 6, tzinfo=UTC)  # older than the default 365-day policy at any date after 2026-08-16


@pytest.fixture
async def world(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    response = await client.post(
        "/api/v1/telemetry/mappings",
        content=(f'{{"integration_id":"{integration["id"]}","source_identifier":"kw","canonical_metric":"power_kw",'
                 '"unit":"kW","scale":1}').encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    return {"collector": collector, "integration": integration, "revision": response.json()["current_revision_id"]}


async def post(client, world, literal, *, at, key=None):
    key = key or uuid.uuid4().hex
    body = (f'{{"records":[{{"dedup_key":"{key}","integration_id":"{world["integration"]["id"]}",'
            f'"external_identifier":"e","source_identifier":"kw","occurred_at":"{at.isoformat()}",'
            f'"mapping_revision_id":"{world["revision"]}","value":{literal}}}]}}').encode()
    collector = world["collector"]
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=body)
    headers["Content-Type"] = "application/json"
    response = await client.post(f"/api/v1/collectors/{collector['id']}/telemetry", content=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"][0], key


async def aggregate_of(db_session):
    return (await db_session.execute(select(DailyTelemetryAggregate).execution_options(populate_existing=True))).scalar_one()


async def count_readings(db_session, key):
    return len((await db_session.execute(select(TelemetryReading).where(TelemetryReading.dedup_key == key))).scalars().all())


async def test_a_late_sample_for_a_compacted_day_is_merged_exactly_over_http(client, world, db_session):
    first, _ = await post(client, world, "1234567890.12345678", at=OLD_DAY)
    assert first["status"] == "accepted"
    assert await compact_eligible_raw(db_session) == 1                       # real clock, default 365-day policy
    await db_session.commit()
    ack, key = await post(client, world, "0.00000001", at=OLD_DAY + timedelta(minutes=5))
    assert ack["status"] == "accepted"
    assert await count_readings(db_session, key) == 0                        # merged into the aggregate, not stored raw
    agg = await aggregate_of(db_session)
    assert (agg.arithmetic_basis, agg.sample_count) == ("decimal_exact", 2)
    assert agg.sum_value == D("1234567890.12345679")
    assert (agg.minimum_value, agg.maximum_value) == (D("0.00000001"), D("1234567890.12345678"))
    assert agg.average_value == D("617283945.06172840")                      # 617283945.061728395 -> half-even to even


async def test_an_exhausted_aggregate_refuses_one_record_which_stays_queueable_while_peers_persist(client, world, db_session):
    await post(client, world, "0", at=OLD_DAY)
    reading = (await db_session.execute(select(TelemetryReading))).scalar_one()
    await compact_eligible_raw(db_session)
    agg = await aggregate_of(db_session)
    agg.sample_count = MAX_COUNT                                              # sum 0, min 0, max 0: still consistent
    await db_session.commit()
    ack, refused_key = await post(client, world, "0", at=OLD_DAY + timedelta(minutes=5))
    assert (ack["status"], ack["error"]) == ("rejected", "AGGREGATE_ARITHMETIC_LIMIT")
    assert classify_telemetry_ack(ack) == "retry"                              # the collector keeps the record
    assert await count_readings(db_session, refused_key) == 0                  # the savepoint discarded it
    peer, peer_key = await post(client, world, "7", at=datetime.now(UTC) + timedelta(seconds=1))
    assert peer["status"] == "accepted" and await count_readings(db_session, peer_key) == 1
    again = await aggregate_of(db_session)
    assert (again.sample_count, again.sum_value, again.arithmetic_basis) == (MAX_COUNT, D("0"), "decimal_exact")
    assert reading.series_key == again.series_key
