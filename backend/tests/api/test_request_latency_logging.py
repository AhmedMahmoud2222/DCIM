import uuid

from httpx import ASGITransport, AsyncClient


async def test_request_completion_log_uses_bounded_route_and_omits_query(client, monkeypatch):
    from app.core import correlation

    captured = []
    monkeypatch.setattr(correlation.logger, "info", lambda event, **fields: captured.append((event, fields)))

    response = await client.get("/api/v1/health/live?credential=query-secret-value")

    assert response.status_code == 200
    assert len(captured) == 1
    event, fields = captured[0]
    assert event == "api_request_completed"
    assert fields["method"] == "GET"
    assert fields["route"] == "/health/live"
    assert fields["status_code"] == 200
    assert fields["duration_ms"] >= 0
    assert "query-secret-value" not in repr(fields)


async def test_escaped_exception_is_counted_and_logged_without_request_data(monkeypatch):
    from app.core import correlation
    from app.core.request_metrics import snapshot
    from app.main import app

    completion_logs = []
    monkeypatch.setattr(
        correlation.logger,
        "info",
        lambda event, **fields: completion_logs.append((event, fields)),
    )

    async def raise_unhandled_error():
        raise RuntimeError("sensitive exception details")

    path = f"/__test_unhandled/{uuid.uuid4()}"
    app.add_api_route(path, raise_unhandled_error, methods=["GET"])
    route = next(route for route in app.router.routes if getattr(route, "path", None) == path)

    def server_error_count():
        return sum(
            item["count"] for item in snapshot()["requests_total"] if item["method"] == "GET" and item["status_class"] == "5xx"
        )

    before = server_error_count()
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"{path}?credential=do-not-record")
        after = snapshot()
    finally:
        app.router.routes.remove(route)

    assert response.status_code == 500
    assert server_error_count() == before + 1
    assert "do-not-record" not in repr(after)
    assert "sensitive exception details" not in repr(after)
    assert len(completion_logs) == 1
    event, fields = completion_logs[0]
    assert event == "api_request_completed"
    assert fields["status_code"] == 500
    assert fields["route"] == path
    assert fields["duration_ms"] >= 0
    assert "do-not-record" not in repr(fields)
    assert "sensitive exception details" not in repr(fields)
