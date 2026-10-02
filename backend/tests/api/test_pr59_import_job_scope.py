"""SEC-RBAC-59-04 (Issue #66): import jobs carry no site link, so a site-restricted caller
may only touch jobs they uploaded. Fails against PR #59's head without the fix."""

import uuid

import pytest

from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from tests.api.test_user_groups import _group, _group_user, _make_site

DATA_PERMS = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _job(client, admin, db_session, import_type="rack"):
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()["id"]
    job = BulkImportJob(
        import_type=import_type, mode="create_only", status="validated", uploaded_by_user_id=uuid.UUID(me),
        original_filename="racks.xlsx", file_hash="0" * 64, file_size_bytes=10, row_count=1, valid_row_count=1,
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add(BulkImportRow(job_id=job.id, row_number=2, status="valid", raw_data={"asset_tag": "OTHER-SITE-RACK"}))
    await db_session.commit()
    return job


@pytest.mark.parametrize("import_type", ["rack", "equipment"])
async def test_restricted_user_cannot_touch_a_job_they_did_not_upload(client, admin, db_session, import_type):
    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": site["site"], "rack_scope": "all"}])
    _, restricted = await _group_user(client, admin, [gid])
    job = await _job(client, admin, db_session, import_type)
    foreign = await client.get(f"/api/v1/import-jobs/{uuid.uuid4()}", headers=restricted)
    for method, suffix in (("get", ""), ("get", "/rows"), ("get", "/report"), ("post", "/commit"), ("post", "/cancel")):
        resp = await client.request(method, f"/api/v1/import-jobs/{job.id}{suffix}", headers=restricted)
        assert resp.status_code == 404, (suffix, resp.status_code)
        assert resp.json()["title"] == foreign.json()["title"]  # indistinguishable from a missing job


async def test_unrestricted_users_keep_access_to_jobs(client, admin, db_session, auth_headers):
    job = await _job(client, admin, db_session)
    manager = await auth_headers("DCIM Manager")
    assert (await client.get(f"/api/v1/import-jobs/{job.id}", headers=manager)).status_code == 200
    assert (await client.get(f"/api/v1/import-jobs/{job.id}/rows", headers=admin)).status_code == 200
