"""Issue #103 area A: deterministic correlation that references, and never alters, source events."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.application import collector_health_sweep as sweep
from app.application import correlation_service as cs
from app.domain.operations.models import CollectorTransition, CorrelationIncident, CorrelationIncidentMember, NotificationDelivery
from app.domain.power.models import ProtectionDevice
from tests.api._ops_helpers import NOW, assign, at, make_alarm, make_collector, make_integration, make_rule
from tests.api._phase2_helpers import create_equipment
from tests.api._phase3_helpers import connection_body, create_room_and_site, create_ups
from tests.api.test_collector_offline_events import make_policy

P = "/api/v1/power"


async def snapshot(db) -> str:
    return (
        await db.execute(
            text(
                "select md5(coalesce((select string_agg(a::text, '|' order by a.id::text) from alarm a), '') || "
                "coalesce((select string_agg(r::text, '|' order by r.id::text) from alarm_rule r), '') || "
                "coalesce((select string_agg(t::text, '|' order by t.id::text) from collector_transition t), '') || "
                "coalesce((select string_agg(h::text, '|' order by h.id::text) from collector_heartbeat h), ''))"
            )
        )
    ).scalar_one()


async def members(db, incident_id):
    rows = (await db.execute(select(CorrelationIncidentMember).where(CorrelationIncidentMember.incident_id == incident_id))).scalars().all()
    return {(m.member_type, m.alarm_id or m.transition_id): m.role for m in rows}


async def run(db, **kw):
    return await cs.correlate(db, now=kw.pop("now", NOW + timedelta(minutes=10)), **kw)


@pytest.mark.asyncio
async def test_collector_offline_groups_its_alarms_and_leaves_others_alone(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    col = await make_collector(db_session, site_id=sid, name="edge-1")
    other_col = await make_collector(db_session, site_id=sid, name="edge-2")
    i1, i2, i3 = [await make_integration(db_session, site_id=sid) for _ in range(3)]
    await assign(db_session, col, i1)
    await assign(db_session, col, i2)
    await assign(db_session, other_col, i3)
    rule = {i.id: await make_rule(db_session, i) for i in (i1, i2, i3)}
    det = NOW - timedelta(minutes=2)
    db_session.add(
        CollectorTransition(
            id=uuid.uuid4(), collector_id=col.id, collector_name="edge-1", site_id=sid, generation=2, from_state="online",
            to_state="offline", detected_at=det, heartbeat_at=det - timedelta(minutes=6), never_heartbeat=False,
            correlation_id="collector-health:x:2",
        )
    )
    await db_session.commit()
    a1 = await make_alarm(db_session, i1, rule[i1.id], opened_at=det + timedelta(seconds=30))
    a2 = await make_alarm(db_session, i2, rule[i2.id], opened_at=det - timedelta(seconds=100))
    unrelated = await make_alarm(db_session, i3, rule[i3.id], opened_at=det + timedelta(seconds=10))
    too_late = await make_alarm(db_session, i1, rule[i1.id], opened_at=det + timedelta(seconds=cs.WINDOW_SECONDS + 1))
    before = await snapshot(db_session)

    summary = await run(db_session)
    assert len(summary.created) == 1
    inc = await db_session.get(CorrelationIncident, summary.created[0])
    assert (inc.rule, inc.cause_type, inc.confidence, inc.status, inc.site_id) == ("collector_offline", "collector_transition", "high", "open", sid)
    assert inc.cause_label == "Collector edge-1 offline" and "edge-1" in inc.rationale
    m = await members(db_session, inc.id)
    t_id = (await db_session.execute(select(CollectorTransition.id))).scalar_one()
    assert m == {("collector_transition", t_id): "cause", ("alarm", a1.id): "symptom", ("alarm", a2.id): "symptom"}
    assert ("alarm", unrelated.id) not in m and ("alarm", too_late.id) not in m
    assert inc.correlation_id.startswith("incident:") and inc.causation_id == str(t_id)
    assert {e["type"] for e in inc.evidence} == {"collector_offline", "alarm"}
    assert await snapshot(db_session) == before  # no source fact changed


@pytest.mark.asyncio
async def test_window_boundary_is_inclusive_at_exactly_the_window(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    col = await make_collector(db_session, site_id=sid)
    i1 = await make_integration(db_session, site_id=sid)
    await assign(db_session, col, i1)
    rule = await make_rule(db_session, i1)
    det = NOW - timedelta(minutes=3)
    db_session.add(CollectorTransition(id=uuid.uuid4(), collector_id=col.id, collector_name="c", site_id=sid, generation=2,
                                       from_state="online", to_state="offline", detected_at=det, never_heartbeat=False))
    await db_session.commit()
    edge = await make_alarm(db_session, i1, rule, opened_at=det + timedelta(seconds=cs.WINDOW_SECONDS))
    await make_alarm(db_session, i1, rule, opened_at=det + timedelta(seconds=cs.WINDOW_SECONDS, milliseconds=1))
    inc_id = (await run(db_session)).created[0]
    m = await members(db_session, inc_id)
    assert ("alarm", edge.id) in m and len([k for k in m if k[0] == "alarm"]) == 1


async def power_plant(client, auth_headers, db_session):
    h = await auth_headers("DCIM Manager")
    room_id, site = await create_room_and_site(client, auth_headers)
    ups = await create_ups(client, h, room_id)
    brk = (
        await client.post(
            f"{P}/protection-devices",
            json={"housing_asset_id": ups["managed_asset_id"], "site_id": site, "label": "BRK-1", "rating_a": 32, "voltage_v": 230, "poles": 1, "phase_config": "single"},
            headers=h,
        )
    ).json()
    eq = {}
    for name in ("a", "b", "c"):
        e = await create_equipment(client, h, auth_headers)
        feed = (await client.post(f"{P}/equipment-feeds", json={"equipment_asset_id": e["id"], "label": f"feed-{name}"}, headers=h)).json()
        eq[name] = uuid.UUID(e["id"])
        src = ups["id"] if name == "c" else brk["id"]
        if name != "c":
            await client.post(f"{P}/connections", json=connection_body(ups["id"], brk["id"]), headers=h) if name == "a" else None
        r = await client.post(f"{P}/connections", json=connection_body(src, feed["id"]), headers=h)
        assert r.status_code == 201, r.text
    integ = await make_integration(db_session, site_id=uuid.UUID(site))
    rule = await make_rule(db_session, integ, rule_type="threshold_high", metric="power_kw")
    return {"site": uuid.UUID(site), "ups": ups, "brk": brk, "eq": eq, "integ": integ, "rule": rule, "h": h}


async def trip(db, brk_id, when):
    await db.execute(update(ProtectionDevice).where(ProtectionDevice.power_node_id == uuid.UUID(brk_id)).values(state="tripped", state_changed_at=when))
    await db.commit()


@pytest.mark.asyncio
async def test_tripped_breaker_groups_downstream_alarms_with_topology_evidence(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    await trip(db_session, p["brk"]["id"], NOW - timedelta(seconds=60))
    a = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=30), asset_id=p["eq"]["a"])
    b = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=20), asset_id=p["eq"]["b"])
    c = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=25), asset_id=p["eq"]["c"])  # not behind the breaker
    before = await snapshot(db_session)
    summary = await run(db_session)
    assert len(summary.created) == 1
    inc = await db_session.get(CorrelationIncident, summary.created[0])
    assert (inc.rule, inc.cause_type, inc.confidence) == ("shared_power_cause", "protection_device", "high")
    assert inc.cause_ref == p["brk"]["id"] and "BRK-1" in inc.cause_label and "tripped" in inc.rationale
    assert await members(db_session, inc.id) == {("alarm", a.id): "symptom", ("alarm", b.id): "symptom"}
    assert any(e["type"] == "protection_state" and e["state"] == "tripped" for e in inc.evidence)
    assert any(e["type"] == "topology_path" for e in inc.evidence)
    assert (await db_session.execute(select(CorrelationIncidentMember).where(CorrelationIncidentMember.alarm_id == c.id))).first() is None
    assert await snapshot(db_session) == before


@pytest.mark.asyncio
async def test_closed_breaker_is_not_a_cause(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=30), asset_id=p["eq"]["a"])
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=20), asset_id=p["eq"]["b"])
    summary = await run(db_session)
    inc = [await db_session.get(CorrelationIncident, i) for i in summary.created]
    assert all(i.rule != "shared_power_cause" for i in inc)  # only the weak same-integration rule may apply
    assert [i.confidence for i in inc] == ["low"]


@pytest.mark.asyncio
async def test_alarmed_upstream_asset_is_the_cause(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    ups_alarm = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=90), asset_id=uuid.UUID(p["ups"]["managed_asset_id"]))
    a = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=30), asset_id=p["eq"]["a"])
    inc = await db_session.get(CorrelationIncident, (await run(db_session)).created[0])
    assert (inc.rule, inc.cause_type, inc.confidence, inc.cause_ref) == ("shared_power_cause", "alarm", "medium", str(ups_alarm.id))
    assert await members(db_session, inc.id) == {("alarm", ups_alarm.id): "cause", ("alarm", a.id): "symptom"}


@pytest.mark.asyncio
async def test_unrelated_alarms_stay_separate(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    i1, i2 = await make_integration(db_session, site_id=sid), await make_integration(db_session, site_id=sid)
    r1, r2 = await make_rule(db_session, i1), await make_rule(db_session, i2)
    await make_alarm(db_session, i1, r1, opened_at=NOW - timedelta(seconds=60))
    await make_alarm(db_session, i2, r2, opened_at=NOW - timedelta(seconds=50))
    await make_alarm(db_session, i1, r1, opened_at=NOW - timedelta(hours=3))  # same integration, far apart in time
    summary = await run(db_session)
    assert summary.created == [] and summary.extended == []
    assert (await db_session.execute(select(func.count()).select_from(CorrelationIncident))).scalar_one() == 0


@pytest.mark.asyncio
async def test_same_device_and_same_integration_are_low_confidence(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    integ = await make_integration(db_session, site_id=sid)
    rule = await make_rule(db_session, integ, rule_type="threshold_high", metric="temperature_c")
    asset = uuid.uuid4()  # no FK needed: managed_asset_id is SET NULL on delete but must exist -> use a real asset below
    from tests.api._phase2_helpers import create_equipment as mk

    h = await auth_headers("DCIM Manager")
    asset = uuid.UUID((await mk(client, h, auth_headers))["id"])
    first = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=100), asset_id=asset)
    second = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=40), asset_id=asset)
    summary = await run(db_session)
    inc = await db_session.get(CorrelationIncident, summary.created[0])
    assert (inc.rule, inc.confidence, inc.cause_ref) == ("same_device", "low", str(first.id))
    assert await members(db_session, inc.id) == {("alarm", first.id): "cause", ("alarm", second.id): "symptom"}
    assert "No topology link" in inc.rationale


@pytest.mark.asyncio
async def test_replay_is_idempotent_and_new_symptoms_extend_the_same_incident(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    await trip(db_session, p["brk"]["id"], NOW - timedelta(seconds=60))
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=30), asset_id=p["eq"]["a"])
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=20), asset_id=p["eq"]["b"])
    first = await run(db_session)
    again = await run(db_session)
    again2 = await run(db_session, now=NOW + timedelta(hours=1))
    assert len(first.created) == 1 and again.created == [] and again.extended == [] and again2.created == []
    assert (await db_session.execute(select(func.count()).select_from(CorrelationIncident))).scalar_one() == 1

    third_eq = await db_session.execute(text("select 1"))  # no-op to keep the session simple
    c = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=10), asset_id=p["eq"]["c"])
    inc_id = first.created[0]
    # connect equipment c behind the breaker is not modelled; instead add another alarm on equipment a (same device)
    a2 = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=5), asset_id=p["eq"]["a"])
    ext = await run(db_session)
    assert ext.extended == [inc_id] and ext.created == []
    inc = await db_session.get(CorrelationIncident, inc_id, populate_existing=True)
    assert inc.version == 2 and ("alarm", a2.id) in await members(db_session, inc_id)
    assert (await db_session.execute(select(CorrelationIncidentMember).where(CorrelationIncidentMember.alarm_id == c.id))).first() is None
    assert third_eq is not None


@pytest.mark.asyncio
async def test_insertion_order_does_not_change_the_result(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    await trip(db_session, p["brk"]["id"], NOW - timedelta(seconds=60))
    # inserted newest first, with a symptom older than the device state change (out-of-order arrival)
    b = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=20), asset_id=p["eq"]["b"])
    a = await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=150), asset_id=p["eq"]["a"])
    inc = await db_session.get(CorrelationIncident, (await run(db_session)).created[0])
    assert inc.dedup_key.startswith("shared_power_cause:device:")
    assert set((await members(db_session, inc.id)).keys()) == {("alarm", a.id), ("alarm", b.id)}
    key = inc.dedup_key
    # wipe incidents and rebuild: identical key
    await db_session.execute(text("delete from correlation_incident"))
    await db_session.commit()
    again = await db_session.get(CorrelationIncident, (await run(db_session)).created[0])
    assert again.dedup_key == key


@pytest.mark.asyncio
async def test_an_event_belongs_to_at_most_one_incident_in_the_database(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    integ = await make_integration(db_session, site_id=uuid.UUID(site))
    rule = await make_rule(db_session, integ)
    a1 = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=30))
    a2 = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=20))
    inc_id = (await run(db_session)).created[0]
    from sqlalchemy.exc import IntegrityError

    other = CorrelationIncident(
        id=uuid.uuid4(), dedup_key="manual", rule="same_device", cause_type="alarm", cause_ref="x", cause_label="x",
        confidence="low", rationale="r", evidence=[], status="open", opened_at=NOW, last_member_at=NOW, correlation_id="c",
        method_version="1", version=1,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(CorrelationIncidentMember(id=uuid.uuid4(), incident_id=other.id, member_type="alarm", alarm_id=a1.id, role="cause", source_time=NOW, added_at=NOW))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    assert inc_id and a2


@pytest.mark.asyncio
async def test_concurrent_runs_create_one_incident(client, auth_headers, db_session, db_engine):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    _, site = await create_room_and_site(client, auth_headers)
    integ = await make_integration(db_session, site_id=uuid.UUID(site))
    rule = await make_rule(db_session, integ)
    await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=30))
    await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=20))
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def go():
        async with factory() as s:
            return await cs.correlate(s, now=NOW + timedelta(minutes=10))

    results = await asyncio.gather(*[go() for _ in range(4)])
    assert sum(len(r.created) for r in results) == 1
    assert (await db_session.execute(select(func.count()).select_from(CorrelationIncident))).scalar_one() == 1


@pytest.mark.asyncio
async def test_new_incident_queues_one_notification_and_replay_adds_none(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    await make_policy(db_session, events=("incident.opened",))
    integ = await make_integration(db_session, site_id=sid)
    rule = await make_rule(db_session, integ)
    await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=30))
    await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=20))
    await run(db_session)
    await run(db_session)
    rows = (await db_session.execute(select(NotificationDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "incident.opened" and rows[0].incident_id is not None
    assert rows[0].payload["probable_cause"].startswith("First alarm")
    assert rows[0].correlation_id.startswith("incident:")


@pytest.mark.asyncio
async def test_sweep_then_correlate_end_to_end(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    sid = uuid.UUID(site)
    col = await make_collector(db_session, site_id=sid, created=NOW - timedelta(days=2))
    integ = await make_integration(db_session, site_id=sid)
    await assign(db_session, col, integ)
    rule = await make_rule(db_session, integ)
    from app.domain.integration.models import CollectorHeartbeat

    db_session.add(CollectorHeartbeat(id=uuid.uuid4(), collector_id=col.id, ts=at(400), status="ok"))
    await db_session.commit()
    await sweep.sweep_collector_states(db_session, now=NOW)
    await make_alarm(db_session, integ, rule, opened_at=NOW + timedelta(seconds=20))
    summary = await run(db_session)
    inc = await db_session.get(CorrelationIncident, summary.created[0])
    assert inc.rule == "collector_offline"


@pytest.mark.asyncio
async def test_a_late_alarm_joins_an_existing_weak_incident_without_changing_its_identity(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    integ = await make_integration(db_session, site_id=uuid.UUID(site))
    rule = await make_rule(db_session, integ)
    first = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=200))
    await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=150))
    created = (await run(db_session)).created[0]
    key = (await db_session.get(CorrelationIncident, created)).dedup_key
    late = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=100))
    older = await make_alarm(db_session, integ, rule, opened_at=NOW - timedelta(seconds=260))  # arrives last, happened first
    ext = await run(db_session)
    assert ext.extended == [created] and ext.created == []
    inc = await db_session.get(CorrelationIncident, created, populate_existing=True)
    assert inc.dedup_key == key and inc.cause_ref == str(first.id)  # the established cause does not move
    assert ("alarm", late.id) in await members(db_session, created)
    assert (await members(db_session, created)).get(("alarm", older.id)) == "symptom"
