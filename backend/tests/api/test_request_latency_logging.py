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
