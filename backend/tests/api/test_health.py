async def test_liveness_does_not_touch_dependencies(client):
    resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


async def test_readiness_reports_database_and_redis(client):
    resp = await client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dependencies"]["database"] == "up"
    assert body["dependencies"]["redis"] == "up"
    assert body["status"] == "ready"
