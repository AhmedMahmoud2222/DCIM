"""Issue #150 M4: atomic PDU instantiation, end to end against real PostgreSQL —
`POST /power/pdus/instantiate` creating `ManagedAsset(asset_type='pdu')` + the `pdu`
graph `PowerNode` + the `PDU` subtype + one `pdu_outlet` node/`PDUOutlet` pair per
published outlet template, in one transaction.

This repo's `tests/api` convention (test_equipment_instantiation.py is the direct
sibling): real `client`, real `make_user`/`auth_headers` RBAC, no mocks, no fake
administrators. The rejections asserted here — draft, non-pdu category, retired without
override, Operator without `power:manage` — are the exact contract
app/application/pdu_instantiation_service.py documents, and the row-level asserts prove
the snapshot semantics: instantiated PDU state comes from the published revision, never
from the request body, and the power domain never points back at the catalog."""

import uuid

from httpx import AsyncClient, Response
from sqlalchemy import func, select

from app.domain.audit.models import AuditLog
from app.domain.identity.models import ManagedAsset
from app.domain.outbox.models import OutboxEvent
from app.domain.power.models import PDU, PDUOutlet, PowerNode


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_manufacturer(client, headers) -> str:
    resp = await client.post(
        "/api/v1/catalog/manufacturers", json={"name": f"PduInstMfr-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_pdu_draft(client, headers) -> tuple[str, dict]:
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "pdu", "model_name": f"PduInst-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    rev_resp = await client.post(f"/api/v1/catalog/models/{model_resp.json()['id']}/revisions", headers=headers)
    assert rev_resp.status_code == 201, rev_resp.text
    return model_resp.json()["id"], rev_resp.json()


def _outlet(outlet_number: int, **overrides) -> dict:
    payload = {
        "stable_key": f"outlet-{outlet_number}",
        "outlet_number": outlet_number,
        "outlet_type": "iec_c13",
        "outlet_count": 1,
        "phase": "single",
        "rated_voltage_v": 230.0,
        "rated_current_a": 10.0,
        "side": "front",
        "sort_order": outlet_number,
    }
    payload.update(overrides)
    return payload


async def _author_electrical(client, headers, revision: dict, *, outlets: list[dict]) -> dict:
    resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        json={
            "pdu_input_voltage_v": 400.0,
            "pdu_input_current_a": 32.0,
            "pdu_phase": "three",
            "mounting_orientation": "vertical",
            "outlets": outlets,
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _make_published_pdu_revision(
    client, headers, *, outlets: list[dict] | None = None
) -> dict:
    """manufacturer -> category='pdu' model -> draft revision -> electrical authoring ->
    publish through the real transactional path (Issue #150 M3)."""
    _, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(
        client, headers, revision, outlets=outlets or [_outlet(1, label="C13-A"), _outlet(2, label="C13-B")]
    )
    publish = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish.status_code == 200, publish.text
    return publish.json()


async def _instantiate(client, headers, revision_id: str, **overrides) -> Response:
    body = {
        "asset_tag": f"PDU-{uuid.uuid4().hex[:8]}",
        "name": "R11-PDU-A",
        "catalog_model_revision_id": revision_id,
    }
    body.update(overrides)
    return await client.post("/api/v1/power/pdus/instantiate", json=body, headers=headers)


# ------------------------------------------------------------------------- Happy path


async def test_instantiate_creates_asset_node_pdu_and_outlet_children(client: AsyncClient, auth_headers, db_session):
    headers = await _admin(auth_headers)
    revision = await _make_published_pdu_revision(client, headers)

    resp = await _instantiate(
        client, headers, revision["id"], asset_tag="PDU-HAPPY-01", ip_address="10.20.30.40", protocol="snmp",
        name="R11-PDU-A",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    asset_id = body["managed_asset_id"]
    assert asset_id is not None
    assert body["node_type"] == "pdu"
    assert body["catalog_model_revision_id"] == revision["id"]
    assert body["outlet_count"] == 2
    assert {o["label"] for o in body["outlets"]} == {"C13-A", "C13-B"}
    assert {o["outlet_number"] for o in body["outlets"]} == {1, 2}
    assert all(o["node_type"] == "pdu_outlet" and o["owning_asset_id"] == asset_id for o in body["outlets"])

    # Row-level snapshot: electrical spec copied from the published revision, not the request.
    asset = await db_session.get(ManagedAsset, uuid.UUID(asset_id))
    assert asset is not None
    assert asset.asset_type == "pdu"
    assert asset.asset_tag == "PDU-HAPPY-01"
    assert asset.lifecycle_status == "planned"
    pdu = await db_session.get(PDU, uuid.UUID(asset_id))
    assert pdu is not None
    assert pdu.name == "R11-PDU-A"
    assert pdu.protocol == "snmp"
    assert pdu.ip_address == "10.20.30.40"
    assert float(pdu.input_voltage) == 400.0
    assert float(pdu.rated_current_a) == 32.0
    assert pdu.outlet_count == 2
    assert pdu.version == 1

    outlet_nodes = list(
        (
            await db_session.execute(
                select(PowerNode)
                .where(PowerNode.node_type == "pdu_outlet", PowerNode.owning_asset_id == uuid.UUID(asset_id))
                .order_by(PowerNode.label)
            )
        ).scalars()
    )
    outlets = list(
        (
            await db_session.execute(
                select(PDUOutlet)
                .where(PDUOutlet.pdu_asset_id == uuid.UUID(asset_id))
                .order_by(PDUOutlet.outlet_number)
            )
        ).scalars()
    )
    assert [o.outlet_number for o in outlets] == [1, 2]
    assert [o.label for o in outlets] == ["C13-A", "C13-B"]
    assert all(o.state == "unknown" for o in outlets)
    assert {o.power_node_id for o in outlets} == {n.id for n in outlet_nodes}
    assert len(outlet_nodes) == 2

    # Exactly one `pdu` graph node pointing at this asset (the topology join target).
    pdu_nodes = list(
        (
            await db_session.execute(
                select(PowerNode).where(PowerNode.node_type == "pdu", PowerNode.managed_asset_id == uuid.UUID(asset_id))
            )
        ).scalars()
    )
    assert len(pdu_nodes) == 1
    assert pdu_nodes[0].label == "R11-PDU-A"

    # Audit + outbox written in the same transaction as the rows (route boundary).
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "power.pdu.instantiate", AuditLog.entity_id == uuid.UUID(asset_id)
            )
        )
    ).scalars().all()
    assert len(audit) == 1
    outbox = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.event_type == "PDUInstantiated"))
    ).scalars().all()
    assert len(outbox) == 1


async def test_outlet_label_longer_than_64_chars_keeps_full_value_on_the_node(
    client: AsyncClient, auth_headers, db_session
):
    """The legacy `pdu_outlet.label` column is String(64) while catalog labels allow 128 —
    the node keeps the full value, the outlet row carries the display-truncated form."""
    headers = await _admin(auth_headers)
    long_label = "X" * 100
    revision = await _make_published_pdu_revision(client, headers, outlets=[_outlet(1, label=long_label)])

    resp = await _instantiate(client, headers, revision["id"])
    assert resp.status_code == 201, resp.text
    asset_id = uuid.UUID(resp.json()["managed_asset_id"])

    node = (await db_session.execute(select(PowerNode).where(PowerNode.owning_asset_id == asset_id))).scalar_one()
    outlet = (await db_session.execute(select(PDUOutlet).where(PDUOutlet.pdu_asset_id == asset_id))).scalar_one()
    assert node.label == long_label
    assert outlet.label == long_label[:64]
    assert resp.json()["outlets"][0]["label"] == long_label


async def test_instantiate_outlet_count_matches_declared_templates(
    client: AsyncClient, auth_headers, db_session
):
    """One explicitly numbered outlet-template row = one physical outlet: instantiation
    creates exactly one PDUOutlet/PowerNode pair per template, and PDU.outlet_count
    equals the declared template count (never fabricated, never silently ignored)."""
    headers = await _admin(auth_headers)
    for declared_count in (1, 4):
        revision = await _make_published_pdu_revision(
            client, headers, outlets=[_outlet(n, label=f"Outlet {n}") for n in range(1, declared_count + 1)]
        )

        resp = await _instantiate(
            client, headers, revision["id"], asset_tag=f"PDU-COUNT-{declared_count}-{uuid.uuid4().hex[:8]}",
            name=f"R11-PDU-C{declared_count}",
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["outlet_count"] == declared_count, body
        assert len(body["outlets"]) == declared_count
        assert {o["outlet_number"] for o in body["outlets"]} == set(range(1, declared_count + 1))

        asset_id = uuid.UUID(body["managed_asset_id"])
        pdu = await db_session.get(PDU, asset_id)
        assert pdu.outlet_count == declared_count

        outlet_rows = (
            await db_session.execute(
                select(PDUOutlet).where(PDUOutlet.pdu_asset_id == asset_id).order_by(PDUOutlet.outlet_number)
            )
        ).scalars().all()
        outlet_nodes = (
            await db_session.execute(
                select(PowerNode)
                .where(PowerNode.node_type == "pdu_outlet", PowerNode.owning_asset_id == asset_id)
                .order_by(PowerNode.label)
            )
        ).scalars().all()
        assert len(outlet_rows) == declared_count
        assert len(outlet_nodes) == declared_count
        assert [o.outlet_number for o in outlet_rows] == list(range(1, declared_count + 1))
        assert {o.power_node_id for o in outlet_rows} == {n.id for n in outlet_nodes}

        assets = (
            await db_session.execute(select(func.count()).select_from(ManagedAsset).where(ManagedAsset.id == asset_id))
        ).scalar_one()
        assert assets == 1


async def test_instantiate_ignores_template_outlet_count_as_multiplier(
    client: AsyncClient, auth_headers, db_session
):
    """`PDUOutletTemplate.outlet_count` is per-template metadata (default 1), not an
    expansion multiplier: one row still creates exactly one outlet pair."""
    headers = await _admin(auth_headers)
    revision = await _make_published_pdu_revision(
        client, headers, outlets=[_outlet(1, outlet_count=5, label="C13-A")]
    )

    resp = await _instantiate(client, headers, revision["id"], asset_tag=f"PDU-NOMULT-{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outlet_count"] == 1
    assert len(body["outlets"]) == 1

    asset_id = uuid.UUID(body["managed_asset_id"])
    outlet_rows = (
        await db_session.execute(select(PDUOutlet).where(PDUOutlet.pdu_asset_id == asset_id))
    ).scalars().all()
    assert len(outlet_rows) == 1
    assert outlet_rows[0].outlet_count == 5  # metadata preserved on the row


# ------------------------------------------------------------------------- Lifecycle rejections


async def test_instantiate_rejects_a_draft_revision_and_writes_no_rows(
    client: AsyncClient, auth_headers, db_session
):
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)

    resp = await _instantiate(client, headers, revision["id"])
    assert resp.status_code == 409, resp.text
    assert "draft" in resp.json()["detail"]

    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 0


async def test_instantiate_rejects_a_published_rack_revision(client: AsyncClient, auth_headers, db_session):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "rack", "model_name": f"Rack-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    rev_resp = await client.post(f"/api/v1/catalog/models/{model_resp.json()['id']}/revisions", headers=headers)
    revision = rev_resp.json()
    fields_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={
            "dimension_unit": "in", "width_value": 19, "height_value": 73.5, "depth_value": 39.4,
            "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220,
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert fields_resp.status_code == 200, fields_resp.text
    publish = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish.status_code == 200, publish.text

    resp = await _instantiate(client, headers, publish.json()["id"], asset_tag="PDU-RACK-REJECT")
    assert resp.status_code == 409, resp.text
    assert "category" in resp.json()["detail"]

    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 0


async def test_instantiate_rejects_retired_revision_until_installation_is_re_enabled(
    client: AsyncClient, auth_headers, db_session
):
    headers = await _admin(auth_headers)
    revision = await _make_published_pdu_revision(client, headers)

    retire = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/retire", json={"reason": "superseded in the field"}, headers=headers
    )
    assert retire.status_code == 200, retire.text

    denied = await _instantiate(client, headers, revision["id"], asset_tag="PDU-RETIRED-DENY")
    assert denied.status_code == 409, denied.text
    assert "retired" in denied.json()["detail"]
    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 0

    override = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/retire-override",
        json={"allow_installation_when_retired": True, "reason": "keep installing during migration window"},
        headers=headers,
    )
    assert override.status_code == 200, override.text

    allowed = await _instantiate(client, headers, revision["id"], asset_tag="PDU-RETIRED-ALLOW")
    assert allowed.status_code == 201, allowed.text
    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 1


# ------------------------------------------------------------------------- RBAC


async def test_operator_cannot_instantiate_but_engineer_without_catalog_manage_can(
    client: AsyncClient, auth_headers, db_session
):
    admin_headers = await _admin(auth_headers)
    revision = await _make_published_pdu_revision(client, admin_headers)

    operator_headers = await auth_headers("Operator")
    denied = await _instantiate(client, operator_headers, revision["id"], asset_tag="PDU-OPERATOR-DENY")
    assert denied.status_code == 403, denied.text
    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 0

    engineer_headers = await auth_headers("Engineer")
    allowed = await _instantiate(client, engineer_headers, revision["id"], asset_tag="PDU-ENGINEER-OK")
    assert allowed.status_code == 201, allowed.text
    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert assets == 1


# ------------------------------------------------------------------------- Idempotency + input validation


async def test_same_idempotency_key_returns_cached_result_without_new_rows(
    client: AsyncClient, auth_headers, db_session
):
    headers = await _admin(auth_headers)
    revision = await _make_published_pdu_revision(client, headers)
    key = uuid.uuid4().hex
    asset_tag = f"PDU-IDEM-{uuid.uuid4().hex[:8]}"
    payload = {"asset_tag": asset_tag, "name": "R11-PDU-IDEM", "catalog_model_revision_id": revision["id"]}
    idem_headers = {**headers, "Idempotency-Key": key}

    first = await client.post("/api/v1/power/pdus/instantiate", json=payload, headers=idem_headers)
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/power/pdus/instantiate", json=payload, headers=idem_headers)
    assert second.status_code == 201, second.text
    assert second.json() == first.json()

    assets = (
        await db_session.execute(select(func.count()).select_from(ManagedAsset).where(ManagedAsset.asset_tag == asset_tag))
    ).scalar_one()
    assert assets == 1
    outlet_rows = (
        await db_session.execute(
            select(func.count()).select_from(PDUOutlet).where(PDUOutlet.pdu_asset_id == uuid.UUID(first.json()["managed_asset_id"]))
        )
    ).scalar_one()
    assert outlet_rows == 2


async def test_unknown_revision_404_and_invalid_protocol_422(client: AsyncClient, auth_headers):
    headers = await _admin(auth_headers)

    missing = await _instantiate(client, headers, str(uuid.uuid4()))
    assert missing.status_code == 404, missing.text

    revision = await _make_published_pdu_revision(client, headers)
    bad_protocol = await _instantiate(client, headers, revision["id"], protocol="telnet")
    assert bad_protocol.status_code == 422, bad_protocol.text
