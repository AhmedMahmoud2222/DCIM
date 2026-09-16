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


async def test_readiness_returns_503_when_a_dependency_is_down(client, monkeypatch):
    """Finding L3 regression (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
    PHASE1_CORRECTION_REPORT.md): a degraded readiness response must not be HTTP 200 —
    orchestrator/load-balancer readiness probes act on status code, not body content."""
    import app.api.v1.health as health_module

    async def _redis_down() -> bool:
        return False

    monkeypatch.setattr(health_module, "check_redis", _redis_down)

    resp = await client.get("/api/v1/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["redis"] == "down"
    assert body["dependencies"]["database"] == "up"
