"""Pass-through relationships (patch-panel front <-> rear): validation, concurrency control, audit, RBAC."""

import uuid

from sqlalchemy import text

from tests.api import _pass_through_helpers as h
from tests.api._cable_helpers import if_match

PT = h.PASS_THROUGHS


async def test_create_get_list_and_delete_with_audit(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    pp = await h.patch_panel(client, headers, "pt-pp", pairs=2)
    ports = pp["port_by_name"]
    created = await h.pass_through(client, headers, ports["Front01"], ports["Rear01"], label="  Row 1  ")
    assert created["label"] == "Row 1" and created["version"] == 1 and created["equipment_hostname"] == "pt-pp"
    assert sorted(p["port_name"] for p in created["ports"]) == ["Front01", "Rear01"]
    assert (await client.get(f"{PT}/{created['id']}", headers=headers)).json()["id"] == created["id"]
    await h.pass_through(client, headers, ports["Front02"], ports["Rear02"])
    listed = (await client.get(PT, params={"equipment_id": pp["id"]}, headers=headers)).json()
    assert listed["total"] == 2
    assert (await client.delete(f"{PT}/{created['id']}", headers=headers)).status_code == 428
    assert (await client.delete(f"{PT}/{created['id']}", headers=headers | if_match(9))).status_code == 409
    assert (await client.delete(f"{PT}/{created['id']}", headers=headers | if_match(1))).status_code == 204
    assert (await client.get(f"{PT}/{created['id']}", headers=headers)).status_code == 404
    assert (await client.get(PT, headers=headers)).json()["total"] == 1
    actions = (await db_session.execute(text(
        "SELECT action FROM audit_log WHERE entity_type = 'port_pass_through' ORDER BY timestamp"))).scalars().all()
    assert actions == ["pass_through.create", "pass_through.create", "pass_through.delete"]
    members = (await db_session.execute(text("SELECT count(*) FROM port_pass_through_member"))).scalar_one()
    assert members == 2  # deleting the relationship removed its members and nothing else


async def test_validation_refuses_bad_pairs(client, auth_headers):
    headers = await auth_headers("Administrator")
    pp = await h.patch_panel(client, headers, "val-pp", pairs=2)
    other = await h.patch_panel(client, headers, "val-other")
    p, o = pp["port_by_name"], other["port_by_name"]
    post = lambda a, b, **kw: client.post(PT, json={"port_a_id": a, "port_b_id": b, **kw}, headers=headers)  # noqa: E731
    assert (await post(p["Front01"], p["Front01"])).status_code == 422
    assert (await post(p["Front01"], o["Rear01"])).status_code == 422  # different equipment
    assert (await post(p["Front01"], p["Rear01"], label="   ")).status_code == 422
    assert (await post(p["Front01"], str(uuid.uuid4()))).status_code == 404
    assert (await post(p["Front01"], p["Rear01"])).status_code == 201
    assert (await post(p["Front01"], p["Rear02"])).status_code == 409  # a port is in at most one pass-through
    assert (await post(p["Rear02"], p["Rear01"])).status_code == 409
    assert (await client.get(PT, headers=headers)).json()["total"] == 1


async def test_cables_and_pass_throughs_coexist_on_the_same_ports(client, auth_headers):
    headers = await auth_headers("Administrator")
    chain = await h.build_chain(client, headers, panels=1, prefix="co")
    # the cabled front/rear ports keep their cables; removing the pass-through leaves the cables untouched
    pt = chain["pass_throughs"][0]
    assert (await client.delete(f"{PT}/{pt['id']}", headers=headers | if_match(pt["version"]))).status_code == 204
    cables = (await client.get("/api/v1/cables", headers=headers)).json()["items"]
    assert sorted(c["label"] for c in cables) == ["co-C0", "co-C1"]
    trace = (await client.get(h.trace_url(chain["sw_a"]["port_by_name"]["Eth1/1"]), headers=headers)).json()
    assert trace["terminated"] == "end_of_path" and trace["hop_count"] == 1  # no pass-through: the path ends at the panel


async def test_rbac_matrix(client, auth_headers):
    admin = await auth_headers("Administrator")
    pp = await h.patch_panel(client, admin, "rbac-pp")
    ports = pp["port_by_name"]
    created = await h.pass_through(client, admin, ports["Front01"], ports["Rear01"])
    for role, can_write in (("Administrator", True), ("DCIM Manager", True), ("Engineer", True), ("Operator", False), ("Viewer", False)):
        headers = await auth_headers(role)
        assert (await client.get(PT, headers=headers)).status_code == 200, role
        assert (await client.get(f"{PT}/{created['id']}", headers=headers)).status_code == 200
        write = await client.post(PT, json={"port_a_id": ports["Front01"], "port_b_id": ports["Rear01"]}, headers=headers)
        assert write.status_code == (409 if can_write else 403), (role, write.status_code)  # 409: the pair already exists
        removal = await client.delete(f"{PT}/{created['id']}", headers=headers | if_match(99))
        assert removal.status_code == (409 if can_write else 403), (role, removal.status_code)
    assert (await client.get(PT)).status_code == 401
