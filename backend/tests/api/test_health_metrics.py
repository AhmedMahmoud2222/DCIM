async def test_metrics_endpoint_requires_unrestricted_administrator(client, auth_headers):
    unauthenticated = await client.get("/api/v1/health/metrics")
    assert unauthenticated.status_code == 401

    viewer = await auth_headers("Viewer")
    denied = await client.get("/api/v1/health/metrics", headers=viewer)
    assert denied.status_code == 403

    administrator = await auth_headers("Administrator")
    allowed = await client.get("/api/v1/health/metrics", headers=administrator)
    assert allowed.status_code == 200


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
