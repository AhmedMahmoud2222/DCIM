import pytest
from starlette.requests import Request


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


async def test_escaped_exception_is_counted_as_server_error_without_request_data(monkeypatch):
    from app.core import correlation

    observations = []
    monkeypatch.setattr(correlation, "observe_request", lambda **fields: observations.append(fields))
    middleware = correlation.CorrelationMiddleware(app=None)
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/internal/resource",
            "raw_path": b"/internal/resource",
            "query_string": b"credential=do-not-record",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )

    async def raise_unhandled_error(_request):
        raise RuntimeError("sensitive exception details")

    with pytest.raises(RuntimeError, match="sensitive exception details"):
        await middleware.dispatch(request, raise_unhandled_error)

    assert len(observations) == 1
    assert observations[0]["method"] == "GET"
    assert observations[0]["status_code"] == 500
    assert observations[0]["duration_ms"] >= 0
    assert "do-not-record" not in repr(observations)
    assert "sensitive exception details" not in repr(observations)
