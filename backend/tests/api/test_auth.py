from app.api.v1.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, REFRESH_COOKIE_NAME


async def test_login_succeeds_with_correct_credentials(client, make_user):
    await make_user("alice@example.com", "correct horse battery staple", "Administrator")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    assert REFRESH_COOKIE_NAME in resp.cookies
    assert CSRF_COOKIE_NAME in resp.cookies


async def test_login_fails_with_wrong_password(client, make_user):
    await make_user("bob@example.com", "correct horse battery staple", "Viewer")
    resp = await client.post("/api/v1/auth/login", json={"email": "bob@example.com", "password": "wrong password"})
    assert resp.status_code == 401
    assert resp.json()["detail"]  # never a raw stack trace


async def test_login_fails_for_unknown_user(client):
    resp = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever"})
    assert resp.status_code == 401


async def test_me_requires_authentication(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_me_returns_current_user(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    assert "@example.com" in resp.json()["email"]


async def test_refresh_requires_csrf_header(client, make_user):
    await make_user("carol@example.com", "correct horse battery staple", "Viewer")
    await client.post("/api/v1/auth/login", json={"email": "carol@example.com", "password": "correct horse battery staple"})

    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 403


async def test_refresh_rotates_the_refresh_token_and_old_cookie_is_revoked(client, make_user):
    await make_user("carol2@example.com", "correct horse battery staple", "Viewer")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "carol2@example.com", "password": "correct horse battery staple"}
    )
    old_refresh_cookie = login.cookies[REFRESH_COOKIE_NAME]
    csrf_token = login.cookies[CSRF_COOKIE_NAME]

    first_refresh = await client.post("/api/v1/auth/refresh", headers={CSRF_HEADER_NAME: csrf_token})
    assert first_refresh.status_code == 200
    rotated_csrf_token = first_refresh.cookies[CSRF_COOKIE_NAME]

    client.cookies.set(REFRESH_COOKIE_NAME, old_refresh_cookie)
    reuse_attempt = await client.post("/api/v1/auth/refresh", headers={CSRF_HEADER_NAME: rotated_csrf_token})
    assert reuse_attempt.status_code == 401


async def test_logout_revokes_refresh_token(client, make_user):
    await make_user("dave@example.com", "correct horse battery staple", "Viewer")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "dave@example.com", "password": "correct horse battery staple"}
    )
    csrf_token = login.cookies[CSRF_COOKIE_NAME]

    logout = await client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf_token})
    assert logout.status_code == 204

    reuse_attempt = await client.post("/api/v1/auth/refresh", headers={CSRF_HEADER_NAME: csrf_token})
    assert reuse_attempt.status_code in (401, 403)
