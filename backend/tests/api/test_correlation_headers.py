from uuid import UUID


async def test_correlation_headers_preserve_bounded_safe_ids(client):
    response = await client.get(
        "/api/v1/health/live",
        headers={"X-Request-Id": "req.123:abc", "X-Correlation-Id": "trace-456"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "req.123:abc"
    assert response.headers["X-Correlation-Id"] == "trace-456"


async def test_correlation_headers_replace_oversized_and_unsafe_values(client):
    response = await client.get(
        "/api/v1/health/live",
        headers={"X-Request-Id": "r" * 129, "X-Correlation-Id": "trace id"},
    )

    assert response.status_code == 200
    request_id = response.headers["X-Request-Id"]
    correlation_id = response.headers["X-Correlation-Id"]
    UUID(request_id)
    UUID(correlation_id)
    assert request_id != correlation_id


async def test_missing_correlation_id_falls_back_to_safe_request_id(client):
    response = await client.get("/api/v1/health/live", headers={"X-Request-Id": "valid-request"})

    assert response.headers["X-Request-Id"] == "valid-request"
    assert response.headers["X-Correlation-Id"] == "valid-request"
