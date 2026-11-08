"""Shared helpers for the Issue #104 API tests. Celery is not running in tests, so import tasks are executed
synchronously with `.run()`, exactly like tests/api/test_floor_plans.py does (the upload endpoint's own
`.delay()` is still exercised and must not raise)."""

import base64

from app.infrastructure.tasks.floorplan_import import (
    run_floor_plan_import_job,
    run_spatial_import_job,
    validate_and_run_raster_import_job,
)
from tests.api._phase2_helpers import create_rack, create_room

FP = "/api/v1/floor-plans"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def run_job(job_id: str, data: bytes, fmt: str) -> None:
    if fmt == "svg":
        run_floor_plan_import_job.run(job_id, b64(data))
    elif fmt in ("dxf", "vsdx"):
        run_spatial_import_job.run(job_id, b64(data), fmt)
    else:
        validate_and_run_raster_import_job.run(job_id, b64(data), fmt)


async def new_floor_plan(client, headers, room_id: str) -> dict:
    resp = await client.post(FP, json={"room_id": room_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def upload(client, headers, floor_plan_id: str, data: bytes, filename: str, content_type: str = "application/octet-stream"):
    return await client.post(f"{FP}/{floor_plan_id}/upload", files={"file": (filename, data, content_type)}, headers=headers)


async def import_file(client, headers, floor_plan_id: str, data: bytes, filename: str, fmt: str) -> dict:
    """Upload + run the task; returns the (refreshed) job."""
    resp = await upload(client, headers, floor_plan_id, data, filename)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    if not job["deduplicated"]:
        run_job(job["id"], data, fmt)
    return (await client.get(f"{FP}/import-jobs/{job['id']}", headers=headers)).json()


async def candidates(client, headers, job_id: str, **params) -> list[dict]:
    resp = await client.get(f"{FP}/import-jobs/{job_id}/candidates", params={"limit": 200, **params}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def calibrate_declared(client, headers, floor_plan_id: str, job_id: str, **extra) -> dict:
    fp = (await client.get(f"{FP}/{floor_plan_id}", headers=headers)).json()
    resp = await client.post(
        f"{FP}/{floor_plan_id}/calibration", json={"method": "declared_units", "job_id": job_id, **extra},
        headers={**headers, "If-Match": str(fp["version"])},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def patch(client, headers, job_id: str, candidate: dict, body: dict):
    return await client.patch(
        f"{FP}/import-jobs/{job_id}/candidates/{candidate['id']}", json=body, headers={**headers, "If-Match": str(candidate["version"])}
    )


async def accept(client, headers, job_id: str, candidate: dict, body: dict | None = None, *, version: int | None = None):
    return await client.post(
        f"{FP}/import-jobs/{job_id}/candidates/{candidate['id']}/accept", json=body or {},
        headers={**headers, "If-Match": str(candidate["version"] if version is None else version)},
    )


async def fresh(client, headers, job_id: str, candidate_id: str) -> dict:
    return next(c for c in await candidates(client, headers, job_id) if c["id"] == candidate_id)


def by_label(items: list[dict], label: str) -> dict:
    return next(c for c in items if c["effective_label"] == label)


async def place_rack(client, headers, auth_headers, room_id: str, x_mm: int, y_mm: int, *, name: str = "Rack", **extra) -> dict:
    return await create_rack(client, headers, auth_headers, room_id=room_id, x_mm=x_mm, y_mm=y_mm, rotation_deg=0, name=name, **extra)


async def room_with_plan(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    return headers, room_id, await new_floor_plan(client, headers, room_id)


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def concurrent_client():
    """A client whose every request gets its own database session/connection, so requests issued with
    asyncio.gather really are concurrent transactions (the shared `client` fixture serialises on one session).
    Mirrors the pattern in tests/api/test_floor_plans.py's RT-1 tests, restoring (not popping) the prior override."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, pool_size=25, max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        else:
            app.dependency_overrides.pop(get_db, None)
        await engine.dispose()
