import uuid

from sqlalchemy import select

from app.application.access_control import load_effective_access
from app.domain.auth.models import Role, RoleAssignment
from tests.api.test_user_groups import _group_user, _make_site


async def test_metrics_endpoint_requires_unrestricted_administrator(client, auth_headers):
    unauthenticated = await client.get("/api/v1/health/metrics")
    assert unauthenticated.status_code == 401

    viewer = await auth_headers("Viewer")
    denied = await client.get("/api/v1/health/metrics", headers=viewer)
    assert denied.status_code == 403

    administrator = await auth_headers("Administrator")
    allowed = await client.get("/api/v1/health/metrics", headers=administrator)
    assert allowed.status_code == 200


async def test_scoped_administrator_with_global_viewer_cannot_read_metrics(client, auth_headers, db_session):
    administrator = await auth_headers("Administrator")
    site = await _make_site(client, administrator)
    user, headers = await _group_user(client, administrator, [])
    roles = dict((await db_session.execute(select(Role.name, Role.id).where(Role.name.in_(["Viewer", "Administrator"])))).all())
    db_session.add_all(
        [
            RoleAssignment(user_id=uuid.UUID(user["id"]), role_id=roles["Viewer"], scope_type="global", scope_id=None),
            RoleAssignment(
                user_id=uuid.UUID(user["id"]), role_id=roles["Administrator"], scope_type="site", scope_id=uuid.UUID(site["site"])
            ),
        ]
    )
    await db_session.commit()

    user_id = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [user_id]))[user_id]
    assert access.scope.unrestricted
    assert "Administrator" not in access.role_names
    response = await client.get("/api/v1/health/metrics", headers=headers)
    assert response.status_code == 403


async def test_metrics_use_only_bounded_labels_and_exclude_query_values(client, auth_headers):
    admin = await auth_headers("Administrator")
    await client.get("/api/v1/health/live?credential=secret-query-value")

    response = await client.get("/api/v1/health/metrics", headers=admin)

    assert response.status_code == 200
    body = response.json()
    assert any(item["method"] == "GET" and item["status_class"] == "2xx" for item in body["requests_total"])
    assert set(body) == {"requests_total", "request_duration_ms"}
    assert "secret-query-value" not in response.text
    assert "/api/v1/health/live" not in response.text
