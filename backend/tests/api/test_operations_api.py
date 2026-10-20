"""Issue #103 areas F/H: operator API behaviour, secret handling and authorization."""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.application import correlation_service as cs
from app.application import notification_service as ns
from app.domain.operations.models import CorrelationIncident, ItsmTicket, NotificationDelivery
from app.domain.power.models import ProtectionDevice
from tests.api._ops_helpers import NOW, make_alarm, make_integration, make_rule
from tests.api._phase3_helpers import create_room_and_site
from tests.api.test_collector_offline_events import make_collector as make_edge_collector  # noqa: F401
from tests.api.test_correlation import power_plant, snapshot, trip

OPS = "/api/v1/operations"
URL = "https://hooks.example.com/services/T000/B000/XXXX-secret-path"
SIGN = "whsec_super_secret"
PW = "snow-password-123"


async def role_headers(client, db, auth_headers, perms: list[str]) -> dict:
    name = f"role-{uuid.uuid4().hex[:8]}"
    rid = uuid.uuid4()
    await db.execute(text("insert into role (id, name, description, is_system) values (:i, :n, 't', false)"), {"i": rid, "n": name})
    for p in perms:
        res, act = p.split(":")
        await db.execute(
            text("insert into role_permission (role_id, permission_id) select :r, id from permission where resource = :a and action = :b"),
            {"r": rid, "a": res, "b": act},
        )
    await db.commit()
    return await auth_headers(name)


async def incident_with_alarms(db, client, auth_headers):
    _, site = await create_room_and_site(client, auth_headers)
    integ = await make_integration(db, site_id=uuid.UUID(site))
    rule = await make_rule(db, integ)
    a1 = await make_alarm(db, integ, rule, opened_at=NOW - timedelta(seconds=30))
    a2 = await make_alarm(db, integ, rule, opened_at=NOW - timedelta(seconds=10))
    summary = await cs.correlate(db, now=NOW + timedelta(minutes=5))
    return summary.created[0], a1, a2, uuid.UUID(site)


@pytest.mark.asyncio
async def test_incident_list_detail_and_operator_actions_leave_alarms_untouched(client, auth_headers, db_session):
    mgr = await auth_headers("DCIM Manager")
    viewer = await auth_headers("Viewer")
    inc_id, a1, a2, site = await incident_with_alarms(db_session, client, auth_headers)
    before = await snapshot(db_session)

    lst = (await client.get(f"{OPS}/incidents", headers=viewer)).json()
    assert lst["total"] == 1 and lst["items"][0]["member_count"] == 2 and lst["items"][0]["rule"] == "same_integration"
    assert (await client.get(f"{OPS}/incidents", params={"status": "resolved"}, headers=viewer)).json()["total"] == 0
    assert (await client.get(f"{OPS}/incidents", params={"site_id": str(site)}, headers=viewer)).json()["total"] == 1
    assert (await client.get(f"{OPS}/incidents", params={"status": "bogus"}, headers=viewer)).status_code == 422

    d = (await client.get(f"{OPS}/incidents/{inc_id}", headers=viewer)).json()
    assert d["confidence"] == "low" and d["correlation_id"].startswith("incident:") and d["all_sources_cleared"] is False
    roles = {(m["alarm_id"], m["role"]) for m in d["members"]}
    assert roles == {(str(a1.id), "cause"), (str(a2.id), "symptom")}
    assert all(m["alarm_status"] == "ACTIVE" and m["alarm_subject"] for m in d["members"])
    assert d["notifications"] == [] and d["tickets"] == []

    nohdr = await client.post(f"{OPS}/incidents/{inc_id}/acknowledge", headers=mgr)
    assert nohdr.status_code == 428
    assert (await client.post(f"{OPS}/incidents/{inc_id}/acknowledge", headers={**viewer, "If-Match": "1"})).status_code == 403
    ack = await client.post(f"{OPS}/incidents/{inc_id}/acknowledge", headers={**mgr, "If-Match": "1"})
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged" and ack.json()["version"] == 2
    assert (await client.post(f"{OPS}/incidents/{inc_id}/acknowledge", headers={**mgr, "If-Match": "2"})).status_code == 409
    assert (await client.post(f"{OPS}/incidents/{inc_id}/resolve", headers={**mgr, "If-Match": "1"})).status_code == 409  # stale
    res = await client.post(f"{OPS}/incidents/{inc_id}/resolve", headers={**mgr, "If-Match": "2"})
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    assert (await client.post(f"{OPS}/incidents/{inc_id}/resolve", headers={**mgr, "If-Match": "3"})).status_code == 409

    assert await snapshot(db_session) == before  # resolving an incident never touches the alarms
    assert (await db_session.execute(text("select count(*) from alarm where status != 'ACTIVE'"))).scalar_one() == 0
    audit = (await db_session.execute(text("select action from audit_log where entity_id = :i order by timestamp"), {"i": inc_id})).scalars().all()
    assert [a for a in audit if a.startswith("correlation.incident.")][-2:] == ["correlation.incident.acknowledged", "correlation.incident.resolved"]


@pytest.mark.asyncio
async def test_topology_evidence_is_hidden_from_callers_without_power_read(client, auth_headers, db_session):
    p = await power_plant(client, auth_headers, db_session)
    await trip(db_session, p["brk"]["id"], NOW - timedelta(seconds=60))
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=30), asset_id=p["eq"]["a"])
    await make_alarm(db_session, p["integ"], p["rule"], opened_at=NOW - timedelta(seconds=20), asset_id=p["eq"]["b"])
    inc_id = (await cs.correlate(db_session, now=NOW + timedelta(minutes=5))).created[0]
    full = (await client.get(f"{OPS}/incidents/{inc_id}", headers=await auth_headers("Viewer"))).json()
    assert any(e["type"] == "protection_state" for e in full["evidence"]) and "BRK-1" in full["rationale"]
    limited = await role_headers(client, db_session, auth_headers, ["alarm:read"])
    cut = (await client.get(f"{OPS}/incidents/{inc_id}", headers=limited)).json()
    assert not any(e.get("scope") == "power" for e in cut["evidence"])
    assert "BRK-1" not in json.dumps(cut) and "details require power:read" in cut["rationale"]
    assert any(e["type"] == "alarm" for e in cut["evidence"])


@pytest.mark.asyncio
async def test_collector_state_and_transition_history_endpoints(client, auth_headers, db_session):
    from app.application import collector_health_sweep as sweep
    from app.domain.integration.models import CollectorHeartbeat

    _, site = await create_room_and_site(client, auth_headers)
    c = await make_edge_collector(db_session, site_id=uuid.UUID(site), name="edge-api")
    db_session.add(CollectorHeartbeat(id=uuid.uuid4(), collector_id=c.id, ts=NOW - timedelta(seconds=900), status="ok"))
    await db_session.commit()
    await sweep.sweep_collector_states(db_session, now=NOW)
    viewer = await auth_headers("Viewer")
    states = (await client.get(f"{OPS}/collector-states", headers=viewer)).json()
    assert [(s["name"], s["state"], s["generation"]) for s in states] == [("edge-api", "offline", 1)]
    tr = (await client.get(f"{OPS}/collector-transitions", params={"collector_id": str(c.id)}, headers=viewer)).json()
    assert tr["total"] == 1 and tr["items"][0]["from_state"] == "unknown" and tr["items"][0]["to_state"] == "offline"
    assert tr["items"][0]["site_id"] == site
    assert (await client.get(f"{OPS}/collector-transitions", params={"collector_id": str(uuid.uuid4())}, headers=viewer)).json()["total"] == 0


@pytest.mark.asyncio
async def test_channels_are_write_only_validated_audited_and_versioned(client, auth_headers, db_session):
    mgr = await auth_headers("DCIM Manager")
    viewer = await auth_headers("Viewer")
    r = await client.post(f"{OPS}/notification-channels", json={"name": "ops-hook", "url": URL, "signing_secret": SIGN}, headers=mgr)
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["url_display"] == "https://hooks.example.com" and ch["has_signing_secret"] is True and ch["version"] == 1
    assert "XXXX" not in r.text and SIGN not in r.text and "url" not in ch and "signing_secret" not in ch
    for text_ in ((await client.get(f"{OPS}/notification-channels", headers=viewer)).text, (await client.get(f"{OPS}/notification-channels/{ch['id']}", headers=viewer)).text):
        assert "XXXX" not in text_ and SIGN not in text_ and "secret-path" not in text_
    assert (await client.post(f"{OPS}/notification-channels", json={"name": "ops-hook", "url": URL}, headers=mgr)).status_code == 409
    assert (await client.post(f"{OPS}/notification-channels", json={"name": "x", "url": URL}, headers=viewer)).status_code == 403
    for bad in ("http://hooks.example.com/h", "ftp://hooks.example.com/h", "https://user:pw@hooks.example.com/h", "https://hooks.example.com/h#frag", "not a url", "https:///nohost"):
        resp = await client.post(f"{OPS}/notification-channels", json={"name": f"n-{uuid.uuid4().hex[:5]}", "url": bad}, headers=mgr)
        assert resp.status_code == 422, bad
        assert "pw" not in resp.text

    assert (await client.patch(f"{OPS}/notification-channels/{ch['id']}", json={"enabled": False}, headers=mgr)).status_code == 428
    upd = await client.patch(f"{OPS}/notification-channels/{ch['id']}", json={"enabled": False, "clear_signing_secret": True}, headers={**mgr, "If-Match": "1"})
    assert upd.status_code == 200 and upd.json()["enabled"] is False and upd.json()["has_signing_secret"] is False and upd.json()["version"] == 2
    assert (await client.patch(f"{OPS}/notification-channels/{ch['id']}", json={"enabled": True}, headers={**mgr, "If-Match": "1"})).status_code == 409
    assert (await client.patch(f"{OPS}/notification-channels/{uuid.uuid4()}", json={}, headers={**mgr, "If-Match": "1"})).status_code == 404

    audit = json.dumps([dict(x._mapping) for x in (await db_session.execute(text("select to_jsonb(a) as j from audit_log a where action like 'notification.channel.%'"))).all()], default=str)
    assert "notification.channel.create" in audit and "notification.channel.update" in audit
    assert "XXXX" not in audit and SIGN not in audit and "secret-path" not in audit
    stored = (await db_session.execute(text("select url_ciphertext, secret_ciphertext from notification_channel"))).one()
    assert "hooks.example" not in stored[0] and "XXXX" not in stored[0]


@pytest.mark.asyncio
async def test_policies_validate_references_and_events(client, auth_headers, db_session):
    mgr = await auth_headers("DCIM Manager")
    ch = (await client.post(f"{OPS}/notification-channels", json={"name": "c1", "url": URL}, headers=mgr)).json()
    _, site = await create_room_and_site(client, auth_headers)
    ok = await client.post(
        f"{OPS}/notification-policies",
        json={"name": "p1", "channel_id": ch["id"], "event_types": ["incident.opened", "collector.offline", "incident.opened"], "site_id": site, "min_confidence": "medium"},
        headers=mgr,
    )
    assert ok.status_code == 201 and ok.json()["event_types"] == ["collector.offline", "incident.opened"]
    base = {"name": "p2", "channel_id": ch["id"], "event_types": ["incident.opened"]}
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "event_types": ["nope"]}, headers=mgr)).status_code == 422
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "event_types": []}, headers=mgr)).status_code == 422
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "min_confidence": "certain"}, headers=mgr)).status_code == 422
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "channel_id": str(uuid.uuid4())}, headers=mgr)).status_code == 404
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "site_id": str(uuid.uuid4())}, headers=mgr)).status_code == 404
    assert (await client.post(f"{OPS}/notification-policies", json={**base, "name": "p1"}, headers=mgr)).status_code == 409
    pid = ok.json()["id"]
    up = await client.patch(f"{OPS}/notification-policies/{pid}", json={"enabled": False, "event_types": ["collector.online"]}, headers={**mgr, "If-Match": "1"})
    assert up.status_code == 200 and up.json()["enabled"] is False and up.json()["event_types"] == ["collector.online"]


@pytest.mark.asyncio
async def test_delivery_history_and_operator_retry(client, auth_headers, db_session):
    mgr = await auth_headers("DCIM Manager")
    viewer = await auth_headers("Viewer")
    inc_id, *_ = await incident_with_alarms(db_session, client, auth_headers)
    ch = (await client.post(f"{OPS}/notification-channels", json={"name": "c-del", "url": URL}, headers=mgr)).json()
    pol = (await client.post(f"{OPS}/notification-policies", json={"name": "p-del", "channel_id": ch["id"], "event_types": ["incident.opened"]}, headers=mgr)).json()
    d = NotificationDelivery(
        id=uuid.uuid4(), policy_id=uuid.UUID(pol["id"]), channel_id=uuid.UUID(ch["id"]), incident_id=inc_id, dedup_key="manual-1",
        event_type="incident.opened", source_type="incident", source_id=inc_id, correlation_id="c", status="failed", attempts=5,
        max_attempts=5, next_attempt_at=NOW, claim_generation=5, failure_code="HTTP_5XX", final_failure=True, payload={"a": 1},
    )
    db_session.add(d)
    await db_session.commit()
    lst = (await client.get(f"{OPS}/notification-deliveries", params={"status": "failed"}, headers=viewer)).json()
    assert lst["total"] == 1 and lst["items"][0]["failure_code"] == "HTTP_5XX" and "payload" not in lst["items"][0]
    detail = (await client.get(f"{OPS}/incidents/{inc_id}", headers=viewer)).json()
    assert detail["notifications"][0]["status"] == "failed" and detail["notifications"][0]["channel_name"] == "c-del"
    assert (await client.get(f"{OPS}/notification-deliveries", params={"status": "weird"}, headers=viewer)).status_code == 422
    assert (await client.post(f"{OPS}/notification-deliveries/{d.id}/retry", headers=viewer)).status_code == 403
    r = await client.post(f"{OPS}/notification-deliveries/{d.id}/retry", headers=mgr)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["attempts"] == 0
    assert (await client.post(f"{OPS}/notification-deliveries/{d.id}/retry", headers=mgr)).status_code == 409
    assert (await client.post(f"{OPS}/notification-deliveries/{uuid.uuid4()}/retry", headers=mgr)).status_code == 404


@pytest.mark.asyncio
async def test_itsm_connection_is_write_only_and_tickets_are_created_once(client, auth_headers, db_session):
    mgr = await auth_headers("DCIM Manager")
    viewer = await auth_headers("Viewer")
    inc_id, *_ = await incident_with_alarms(db_session, client, auth_headers)
    r = await client.post(
        f"{OPS}/itsm-connections",
        json={"name": "snow-prod", "base_url": "https://acme.service-now.com/", "username": "svc_dcim", "password": PW, "auto_create": False},
        headers=mgr,
    )
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["base_url"] == "https://acme.service-now.com" and conn["has_password"] is True
    assert PW not in r.text and "password" not in {k for k in conn if k != "has_password"}
    assert PW not in (await client.get(f"{OPS}/itsm-connections", headers=viewer)).text
    for bad in ("https://acme.service-now.com/api", "https://u:p@acme.service-now.com", "http://acme.service-now.com", "https://acme.service-now.com?x=1"):
        assert (await client.post(f"{OPS}/itsm-connections", json={"name": f"n{uuid.uuid4().hex[:4]}", "base_url": bad, "username": "u", "password": PW}, headers=mgr)).status_code == 422, bad
    assert (await client.post(f"{OPS}/itsm-connections", json={"name": "snow-prod", "base_url": "https://b.example.com", "username": "u", "password": PW}, headers=mgr)).status_code == 409

    t1 = await client.post(f"{OPS}/incidents/{inc_id}/tickets", json={"connection_id": conn["id"]}, headers=mgr)
    t2 = await client.post(f"{OPS}/incidents/{inc_id}/tickets", json={"connection_id": conn["id"]}, headers=mgr)
    assert t1.status_code == t2.status_code == 202 and t1.json()["id"] == t2.json()["id"]
    assert t1.json()["status"] == "pending" and t1.json()["external_number"] is None
    assert (await db_session.execute(select(ItsmTicket.id))).scalars().all() == [uuid.UUID(t1.json()["id"])]
    assert (await client.post(f"{OPS}/incidents/{inc_id}/tickets", json={"connection_id": conn["id"]}, headers=viewer)).status_code == 403
    assert (await client.post(f"{OPS}/incidents/{inc_id}/tickets", json={"connection_id": str(uuid.uuid4())}, headers=mgr)).status_code == 404
    assert (await client.post(f"{OPS}/incidents/{uuid.uuid4()}/tickets", json={"connection_id": conn["id"]}, headers=mgr)).status_code == 404

    up = await client.patch(f"{OPS}/itsm-connections/{conn['id']}", json={"password": "rotated-pw", "enabled": False}, headers={**mgr, "If-Match": "1"})
    assert up.status_code == 200 and up.json()["enabled"] is False and "rotated-pw" not in up.text
    assert (await client.post(f"{OPS}/incidents/{uuid.uuid4()}/tickets", json={"connection_id": conn["id"]}, headers=mgr)).status_code == 404
    other_inc = (await incident_with_alarms(db_session, client, auth_headers))[0]
    assert (await client.post(f"{OPS}/incidents/{other_inc}/tickets", json={"connection_id": conn["id"]}, headers=mgr)).status_code == 409  # disabled

    tl = (await client.get(f"{OPS}/itsm-tickets", params={"incident_id": str(inc_id)}, headers=viewer)).json()
    assert tl["total"] == 1 and tl["items"][0]["connection_name"] == "snow-prod"
    assert (await client.post(f"{OPS}/itsm-tickets/{t1.json()['id']}/retry", headers=mgr)).status_code == 409  # not failed
    audit = json.dumps([dict(x._mapping) for x in (await db_session.execute(text("select to_jsonb(a) as j from audit_log a where action like 'itsm.%'"))).all()], default=str)
    assert PW not in audit and "rotated-pw" not in audit


@pytest.mark.asyncio
async def test_resolving_an_incident_queues_an_outbound_update_for_existing_tickets(client, auth_headers, db_session):
    from app.application import itsm_service as its
    from tests.api.test_itsm_servicenow import make_connection

    mgr = await auth_headers("DCIM Manager")
    inc_id, *_ = await incident_with_alarms(db_session, client, auth_headers)
    conn = await make_connection(db_session)
    inc = await db_session.get(CorrelationIncident, inc_id)
    tid, _ = await its.ensure_ticket(db_session, inc, conn, now=NOW)
    await db_session.execute(update(ItsmTicket).where(ItsmTicket.id == tid).values(status="synced", pending_op="update", external_id="a" * 32, external_state="new"))
    await db_session.commit()
    res = await client.post(f"{OPS}/incidents/{inc_id}/resolve", headers={**mgr, "If-Match": "1"})
    assert res.status_code == 200
    t = await db_session.get(ItsmTicket, tid, populate_existing=True)
    assert (t.status, t.pending_op, t.attempts) == ("pending", "update", 0)


# --------------------------------------------------------------------------- authorization sweep


def operations_routes():
    from app.main import app

    out = []
    for path, item in app.openapi()["paths"].items():
        if path.startswith("/api/v1/operations"):
            for method in item:
                out.append((method.upper(), path))
    return sorted(out)


def fill(path: str, ident: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", ident, path)


def test_the_route_sweep_sees_every_operations_endpoint():
    routes = operations_routes()
    assert len(routes) == 23  # a new route must be reviewed here and added to the authorization sweeps
    assert ("POST", "/api/v1/operations/incidents/{incident_id}/resolve") in routes
    assert ("GET", "/api/v1/operations/itsm-tickets/{ticket_id}") in routes


@pytest.mark.asyncio
async def test_every_operations_endpoint_requires_authentication(client, db_session):
    for method, path in operations_routes():
        resp = await client.request(method, fill(path, str(uuid.uuid4())), json={} if method in ("POST", "PATCH") else None)
        assert resp.status_code == 401, (method, path, resp.status_code)


@pytest.mark.asyncio
async def test_read_only_roles_cannot_mutate_and_see_no_secrets(client, auth_headers, db_session):
    viewer = await auth_headers("Viewer")
    mgr = await auth_headers("DCIM Manager")
    ch = (await client.post(f"{OPS}/notification-channels", json={"name": "ro", "url": URL, "signing_secret": SIGN}, headers=mgr)).json()
    for method, path in operations_routes():
        if method == "GET":
            continue
        resp = await client.request(method, fill(path, ch["id"]), json={}, headers={**viewer, "If-Match": "1"})
        assert resp.status_code == 403, (method, path, resp.status_code)
    dump = ""
    for method, path in operations_routes():
        if method == "GET" and "{" not in path:
            dump += (await client.get(path, headers=viewer)).text
    assert "XXXX" not in dump and SIGN not in dump and "secret-path" not in dump


@pytest.mark.asyncio
async def test_site_restricted_user_is_denied_everywhere_identically_for_real_and_missing_ids(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    inc_id, *_ , site = await incident_with_alarms(db_session, client, auth_headers)
    ch = (await client.post(f"{OPS}/notification-channels", json={"name": "rs", "url": URL}, headers=await auth_headers("DCIM Manager"))).json()
    gid = (await client.post("/api/v1/groups", json={"name": f"G-{uuid.uuid4().hex[:8]}"}, headers=admin)).json()["id"]
    perms = ["alarm:read", "alarm:manage", "collector:read", "integration:read", "integration:manage", "power:read"]
    assert (await client.put(f"/api/v1/groups/{gid}/permissions", json={"allow": perms, "deny": []}, headers=admin)).status_code == 200
    assert (await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": str(site), "rack_scope": "all"}]}, headers=admin)).status_code == 200
    email, pw = f"u-{uuid.uuid4().hex[:8]}@example.com", "correct horse battery staple"
    assert (await client.post("/api/v1/users", json={"email": email, "full_name": "U", "password": pw, "group_ids": [gid]}, headers=admin)).status_code == 201
    token = (await client.post("/api/v1/auth/login", json={"email": email, "password": pw})).json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}", "If-Match": "1"}
    seen = set()
    for method, path in operations_routes():
        real = await client.request(method, fill(path, str(inc_id)), json={}, headers=hdr)
        missing = await client.request(method, fill(path, str(uuid.uuid4())), json={}, headers=hdr)
        assert real.status_code == missing.status_code == 403, (method, path, real.status_code, missing.status_code)
        volatile = {"request_id", "instance"}  # per-request correlation and the path that was asked for
        assert {k: v for k, v in real.json().items() if k not in volatile} == {
            k: v for k, v in missing.json().items() if k not in volatile
        }
        seen.add((method, path))
    assert len(seen) == 23 and ch["id"]


@pytest.mark.asyncio
async def test_dispatch_after_queueing_does_not_run_inline(client, auth_headers, db_session, monkeypatch):
    """Endpoints that queue work hand ids to Celery after commit; a broker outage never fails the request."""
    mgr = await auth_headers("DCIM Manager")
    inc_id, *_ = await incident_with_alarms(db_session, client, auth_headers)
    conn = (await client.post(f"{OPS}/itsm-connections", json={"name": "sn-x", "base_url": "https://acme.service-now.com", "username": "u", "password": PW}, headers=mgr)).json()

    def down(**kw):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.infrastructure.tasks.itsm.sync_itsm_ticket.apply_async", down)
    r = await client.post(f"{OPS}/incidents/{inc_id}/tickets", json={"connection_id": conn["id"]}, headers=mgr)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert ns.MAX_ATTEMPTS == 5 and ProtectionDevice is not None
