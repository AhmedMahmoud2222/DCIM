"""DB-03 (#357): post-expiry HTTP contract for all five creation endpoints (matrix T26-T27, T36).

An Idempotency-Key is honoured for a bounded window (24 h). After that window a request is executed again:
* the same successful create answers **409** (the permanent `asset_tag` already exists), not the original 201;
* a request with a new, free business key executes normally;
* nothing is duplicated: no second asset, audit row or outbox event, and the claim is released.
See docs/implementation/IDEMPOTENCY_EXPIRY.md.

Real PostgreSQL via the repo's `client`/`db_session` fixtures; expiry is produced by ageing the ledger row in
SQL, never by sleeping."""

import uuid

import pytest
from sqlalchemy import func, select, text

from app.domain.audit.models import AuditLog
from app.domain.identity.models import ManagedAsset
from app.domain.outbox.models import OutboxEvent
from tests.api._phase2_helpers import create_equipment_model_revision, create_rack_model_revision
from tests.api.test_equipment_instantiation import _make_published_equipment_revision
from tests.api.test_pdu_instantiation import _make_published_pdu_revision

CASES = ["managed-assets", "equipment", "equipment-instantiate", "racks", "pdu-instantiate"]


async def _prepare(case: str, client, auth_headers):
    """Returns (path, headers, make_body(tag))."""
    if case == "managed-assets":
        headers = await auth_headers("Engineer")
        return "/api/v1/managed-assets", headers, lambda tag: {"asset_type": "equipment", "asset_tag": tag}
    if case == "equipment":
        headers = await auth_headers("Engineer")
        revision_id = await create_equipment_model_revision(client, auth_headers)
        return "/api/v1/equipment", headers, lambda tag: {
            "asset_tag": tag, "model_revision_id": revision_id, "hostname": "h"
        }
    if case == "racks":
        headers = await auth_headers("Engineer")
        revision_id = await create_rack_model_revision(client, auth_headers)
        return "/api/v1/racks", headers, lambda tag: {"asset_tag": tag, "model_revision_id": revision_id, "name": "R"}
    admin = await auth_headers("Administrator")
    if case == "equipment-instantiate":
        revision = await _make_published_equipment_revision(client, admin, rack_unit_height=1, port_count=1, psu_quantity=0)
        return "/api/v1/equipment/instantiate", admin, lambda tag: {
            "asset_tag": tag, "catalog_model_revision_id": revision["id"], "hostname": "h"
        }
    revision = await _make_published_pdu_revision(client, admin)
    return "/api/v1/power/pdus/instantiate", admin, lambda tag: {
        "asset_tag": tag, "name": "PDU", "catalog_model_revision_id": revision["id"]
    }


async def _expire(db_session, key: str) -> None:
    await db_session.execute(
        text("UPDATE idempotency_key SET expires_at = now() - interval '1 second' WHERE key = :k"), {"k": key}
    )
    await db_session.commit()


async def _counts(db_session, tag: str) -> tuple[int, int, int]:
    assets = (await db_session.execute(select(func.count()).select_from(ManagedAsset).where(ManagedAsset.asset_tag == tag))).scalar_one()
    audits = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    outbox = (await db_session.execute(select(func.count()).select_from(OutboxEvent))).scalar_one()
    return assets, audits, outbox


async def _ledger(db_session, key: str):
    return (
        await db_session.execute(
            text("SELECT status, claim_generation, request_hash FROM idempotency_key WHERE key = :k"), {"k": key}
        )
    ).all()


@pytest.mark.parametrize("case", CASES)
async def test_t26_a_retried_successful_create_after_expiry_is_409_with_no_duplicate_effects(
    case, client, auth_headers, db_session
):
    path, headers, make_body = await _prepare(case, client, auth_headers)
    tag = f"EXP-{uuid.uuid4().hex[:10]}"
    key = str(uuid.uuid4())
    body = make_body(tag)

    first = await client.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    assert first.status_code == 201, first.text
    # Inside the window the original result is replayed.
    replay = await client.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    assert replay.status_code == 201 and replay.json() == first.json()

    before = await _counts(db_session, tag)
    assert before[0] == 1
    await _expire(db_session, key)

    after_expiry = await client.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    assert after_expiry.status_code == 409, after_expiry.text  # not 201, not 422
    assert await _counts(db_session, tag) == before  # no duplicate asset, audit or outbox rows
    assert await _ledger(db_session, key) == []  # the failed re-execution released its claim

    again = await client.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    assert again.status_code == 409  # every further retry behaves the same
    assert await _counts(db_session, tag) == before


@pytest.mark.parametrize("case", CASES)
async def test_t27_an_expired_key_with_a_new_free_business_key_executes_normally(
    case, client, auth_headers, db_session
):
    path, headers, make_body = await _prepare(case, client, auth_headers)
    key = str(uuid.uuid4())
    first_tag = f"EXP-{uuid.uuid4().hex[:10]}"
    assert (await client.post(path, json=make_body(first_tag), headers={**headers, "Idempotency-Key": key})).status_code == 201
    await _expire(db_session, key)

    new_tag = f"EXP-{uuid.uuid4().hex[:10]}"
    resp = await client.post(path, json=make_body(new_tag), headers={**headers, "Idempotency-Key": key})
    assert resp.status_code == 201, resp.text  # previously a permanent 422 "Idempotency Key Reused"
    [(status, generation, _hash)] = await _ledger(db_session, key)
    assert (status, generation) == ("completed", 2)
    assert (await _counts(db_session, new_tag))[0] == 1 and (await _counts(db_session, first_tag))[0] == 1


@pytest.mark.parametrize("case", CASES)
async def test_in_window_reuse_with_a_different_body_is_still_rejected(case, client, auth_headers):
    path, headers, make_body = await _prepare(case, client, auth_headers)
    key = str(uuid.uuid4())
    assert (await client.post(path, json=make_body(f"EXP-{uuid.uuid4().hex[:10]}"), headers={**headers, "Idempotency-Key": key})).status_code == 201
    other = await client.post(path, json=make_body(f"EXP-{uuid.uuid4().hex[:10]}"), headers={**headers, "Idempotency-Key": key})
    assert other.status_code == 422 and other.json()["title"] == "Idempotency Key Reused"
