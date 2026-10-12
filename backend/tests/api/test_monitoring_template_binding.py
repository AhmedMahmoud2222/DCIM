"""G9b binding survives authored API edits/cloning and rejects ambiguous conversion."""

import asyncio

import pytest

from tests.api.test_catalog_designer_lifecycle import _make_draft, _make_manufacturer, _make_model


async def make_revision(client, headers):
    manufacturer = await _make_manufacturer(client, headers)
    model = await _make_model(client, headers, manufacturer, category="equipment")
    return await _make_draft(client, headers, model)


def body(**changes):
    return {
        "stable_key": "temperature", "metric_name": "temperature", "protocol": "snmp",
        "oid": "1.3.6.1.4.1.999.1", "value_type": "float", "unit": "degF", "scale": 0.1,
        "registry_metric": "temperature_c", "registry_version": "2", "threshold_unit": "degF",
        "default_warning_threshold": 95,
    } | changes


async def test_binding_create_partial_edit_rejection_and_read(client, auth_headers):
    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    url = f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics"
    response = await client.post(url, json=body(), headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    metric = response.json()
    assert metric["binding_status"] == "bound"
    assert metric["registry_version"] == "2" and metric["threshold_unit"] == "degF"
    update_url = f"{url}/{metric['id']}"
    response = await client.patch(update_url, json={"transform": "scale"},
                                  headers={**headers, "If-Match": str(metric["revision_version"])})
    assert response.status_code == 422, response.text
    # Rejection rolls the parent version increment back too.
    response = await client.patch(update_url, json={"metric_name": "Authored source temperature"},
                                  headers={**headers, "If-Match": str(metric["revision_version"])})
    assert response.status_code == 200, response.text
    assert response.json()["binding_status"] == "bound"
    read = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["monitoring_metrics"][0]["registry_version"] == "2"


@pytest.mark.parametrize("changes", [
    {"registry_version": None}, {"unit": "W"}, {"threshold_unit": None},
    {"threshold_unit": "kW"}, {"offset": 32}, {"transform": "scale_and_offset"},
])
async def test_invalid_binding_rejected_without_mutation(client, auth_headers, changes):
    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics", json=body(**changes),
                                 headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 422, response.text
    read = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert read.json()["monitoring_metrics"] == []
    assert read.json()["version"] == revision["version"]


async def test_permission_and_unbound_compatibility(client, auth_headers):
    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    url = f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics"
    denied = await client.post(url, json=body(), headers={**await auth_headers("Viewer"), "If-Match": "1"})
    assert denied.status_code == 403, denied.text
    response = await client.post(url, json=body(registry_metric=None, registry_version=None, threshold_unit=None,
                                              unit="vendor-temperature", transform="scale", offset=12),
                                 headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    assert response.json()["binding_status"] == "unbound"


async def test_published_binding_clones_without_reinterpretation(client, auth_headers):
    from tests.api.test_catalog_designer_lifecycle import _fill_required_rack_fields

    headers = await auth_headers("Administrator")
    manufacturer = await _make_manufacturer(client, headers)
    model = await _make_model(client, headers, manufacturer, category="rack")
    revision = await _make_draft(client, headers, model)
    revision = await _fill_required_rack_fields(client, headers, revision)
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics", json=body(),
                                 headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert response.status_code == 200, response.text
    response = await client.post(f"/api/v1/catalog/models/{model}/revisions/clone", headers=headers,
                                 params={"from_revision_id": revision["id"]})
    assert response.status_code == 201, response.text
    cloned = await client.get(f"/api/v1/catalog/revisions/{response.json()['id']}", headers=headers)
    metric = cloned.json()["monitoring_metrics"][0]
    assert (metric["registry_metric"], metric["registry_version"], metric["threshold_unit"], metric["scale"]) == (
        "temperature_c", "2", "degF", 0.1)


async def test_concurrent_binding_edits_have_one_revision_winner(client, auth_headers):
    import os

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app

    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    url = f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics"
    created = await client.post(url, json=body(), headers={**headers, "If-Match": str(revision["version"])})
    assert created.status_code == 201, created.text
    metric = created.json()
    update_url = f"{url}/{metric['id']}"
    concurrent_headers = {**headers, "If-Match": str(metric["revision_version"])}
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def independent_db():
        async with sessions() as session:
            yield session

    original_override = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = independent_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as first, AsyncClient(
            transport=transport, base_url="http://test"
        ) as second:
            responses = await asyncio.gather(
                first.patch(update_url, json={"unit": "degC", "scale": 1}, headers=concurrent_headers),
                second.patch(update_url, json={"threshold_unit": "degC", "default_warning_threshold": 35},
                             headers=concurrent_headers),
            )
    finally:
        app.dependency_overrides[get_db] = original_override
        await engine.dispose()
    assert sorted(response.status_code for response in responses) == [200, 409]
    read = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert read.status_code == 200, read.text
    current = read.json()["monitoring_metrics"][0]
    winner = next(response.json() for response in responses if response.status_code == 200)
    for field in ("unit", "scale", "registry_metric", "registry_version", "threshold_unit", "default_warning_threshold"):
        assert current[field] == winner[field]
