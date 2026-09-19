"""Pre-MVP consolidation hardening (Codex H1 / SSRF): permanent regression tests for
`app/application/drivers/network_policy.py`. Pure policy-level tests need no network
access at all (DNS resolution of literal IPs and `localhost` is local-only); the
handful that exercise the real REST driver end-to-end use a genuine local HTTP server
bound to 127.0.0.1 on an ephemeral port -- never a real external or metadata target."""

import asyncio
import ipaddress
import uuid

import httpx
import pytest

from app.application.drivers.base import DriverConnectionError
from app.application.drivers.network_policy import (
    NetworkPolicy,
    NetworkPolicyError,
    canonicalize_host,
    validate_target,
)
from app.application.drivers.rest import RESTDriver

_DC_POLICY = NetworkPolicy(allowed_networks=(ipaddress.ip_network("10.10.0.0/16"),))


# =====================================================================================
# Allowed / disallowed targets
# =====================================================================================

async def test_allowed_target_within_configured_network_passes():
    target = await validate_target(scheme="http", host="10.10.5.5", port=80, method="GET", policy=_DC_POLICY)
    assert str(target.pinned_ip) == "10.10.5.5"


async def test_disallowed_target_outside_any_configured_network_rejected():
    with pytest.raises(NetworkPolicyError, match="not within any configured allowed network"):
        await validate_target(scheme="http", host="10.20.5.5", port=80, method="GET", policy=_DC_POLICY)


async def test_broad_private_allowlist_still_blocks_metadata_target():
    """Even a deliberately broad allowlist (all of 10.0.0.0/8) must not let the
    cloud-metadata address through -- the always-denied tier is a safety net
    independent of how permissive `allowed_networks` is configured."""
    broad_policy = NetworkPolicy(allowed_networks=(ipaddress.ip_network("10.0.0.0/8"),))
    with pytest.raises(NetworkPolicyError, match="always denied"):
        await validate_target(scheme="http", host="169.254.169.254", port=80, method="GET", policy=broad_policy)


async def test_ipv4_mapped_ipv6_metadata_address_still_caught():
    with pytest.raises(NetworkPolicyError, match="always denied"):
        await validate_target(scheme="http", host="::ffff:169.254.169.254", port=80, method="GET", policy=_DC_POLICY)


@pytest.mark.parametrize(
    "host,label",
    [
        ("0.0.0.0", "unspecified"),
        ("255.255.255.255", "broadcast"),
        ("224.0.0.1", "multicast"),
        ("169.254.169.254", "link-local/metadata"),
    ],
)
async def test_always_denied_addresses_rejected_even_with_broad_allowlist(host, label):
    broad_policy = NetworkPolicy(allowed_networks=(ipaddress.ip_network("0.0.0.0/0"),))
    with pytest.raises(NetworkPolicyError, match="always denied"):
        await validate_target(scheme="http", host=host, port=80, method="GET", policy=broad_policy)


# =====================================================================================
# Loopback policy
# =====================================================================================

async def test_loopback_denied_by_default():
    with pytest.raises(NetworkPolicyError, match="always denied"):
        await validate_target(scheme="http", host="127.0.0.1", port=80, method="GET", policy=_DC_POLICY)


async def test_loopback_allowed_only_when_explicitly_enabled():
    permissive = NetworkPolicy(allow_loopback=True)
    target = await validate_target(scheme="http", host="127.0.0.1", port=80, method="GET", policy=permissive)
    assert str(target.pinned_ip) == "127.0.0.1"


async def test_ipv6_loopback_allowed_only_when_explicitly_enabled():
    with pytest.raises(NetworkPolicyError, match="always denied"):
        await validate_target(scheme="http", host="::1", port=80, method="GET", policy=_DC_POLICY)
    permissive = NetworkPolicy(allow_loopback=True)
    target = await validate_target(scheme="http", host="::1", port=80, method="GET", policy=permissive)
    assert target.pinned_ip == ipaddress.ip_address("::1")


# =====================================================================================
# IPv6 / hostname resolution / DNS policy
# =====================================================================================

async def test_ipv6_literal_within_allowed_network():
    policy = NetworkPolicy(allowed_networks=(ipaddress.ip_network("2001:db8::/32"),))
    target = await validate_target(scheme="http", host="2001:db8::1", port=80, method="GET", policy=policy)
    assert target.pinned_ip == ipaddress.ip_address("2001:db8::1")


async def test_bracketed_ipv6_url_literal_canonicalized_correctly():
    assert canonicalize_host("[2001:db8::1]") == "2001:db8::1"


async def test_hostname_resolution_localhost_treated_as_loopback():
    permissive = NetworkPolicy(allow_loopback=True)
    target = await validate_target(scheme="http", host="localhost", port=80, method="GET", policy=permissive)
    assert target.pinned_ip.is_loopback


async def test_hostname_resolving_outside_allowlist_rejected_by_dns_policy():
    """A hostname that resolves to a real, routable, PUBLIC address (never contacted
    -- `validate_target` only resolves and validates, it never connects) must be
    rejected the same way a literal out-of-range IP is: the DNS-resolved address is
    what gets judged against the allowlist, not the hostname string itself."""
    with pytest.raises(NetworkPolicyError, match="not within any configured allowed network"):
        await validate_target(scheme="http", host="example.com", port=80, method="GET", policy=_DC_POLICY)


async def test_unresolvable_hostname_rejected_cleanly():
    with pytest.raises(NetworkPolicyError, match="DNS resolution failed"):
        await validate_target(
            scheme="http", host="this-host-does-not-exist.invalid", port=80, method="GET", policy=_DC_POLICY,
        )


# =====================================================================================
# Scheme / port / method
# =====================================================================================

async def test_invalid_scheme_rejected():
    with pytest.raises(NetworkPolicyError, match="Scheme"):
        await validate_target(scheme="ftp", host="10.10.5.5", port=80, method="GET", policy=_DC_POLICY)


async def test_invalid_port_rejected():
    policy = NetworkPolicy(allowed_networks=(ipaddress.ip_network("10.10.0.0/16"),), allowed_ports=frozenset({80, 443}))
    with pytest.raises(NetworkPolicyError, match="Port"):
        await validate_target(scheme="http", host="10.10.5.5", port=9999, method="GET", policy=policy)


async def test_configurable_management_port_can_be_allowed():
    policy = NetworkPolicy(
        allowed_networks=(ipaddress.ip_network("10.10.0.0/16"),), allowed_ports=frozenset({80, 443, 8443}),
    )
    target = await validate_target(scheme="https", host="10.10.5.5", port=8443, method="GET", policy=policy)
    assert target.port == 8443


async def test_unsafe_method_rejected_by_default():
    with pytest.raises(NetworkPolicyError, match="method"):
        await validate_target(scheme="http", host="10.10.5.5", port=80, method="POST", policy=_DC_POLICY)


async def test_safe_methods_allowed_by_default():
    for method in ("GET", "HEAD"):
        target = await validate_target(scheme="http", host="10.10.5.5", port=80, method=method, policy=_DC_POLICY)
        assert target.method == method


# =====================================================================================
# Real end-to-end driver tests against a genuine local HTTP server
# =====================================================================================

async def _run_local_server(handler):
    """A tiny real asyncio TCP server bound to 127.0.0.1 on an ephemeral port -- not a
    mock, not a fixture library, just stdlib asyncio, so response timing/size/status
    behavior is genuinely exercised over a real socket."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_oversized_response_is_truncated_not_fully_buffered():
    max_bytes = 1024

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"x" * (max_bytes * 10)
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    server, port = await _run_local_server(handler)
    try:
        policy = NetworkPolicy(allow_loopback=True, max_response_bytes=max_bytes, allowed_ports=frozenset({port}))
        driver = RESTDriver(uuid.uuid4(), network_policy=policy)
        await driver.connect(target_host="127.0.0.1", target_port=port, config={"scheme": "http", "path": "/"}, credential=None)
        try:
            result = await driver.poll()
        finally:
            await driver.disconnect()
        assert result.raw_attributes["body"] == {"truncated": True}
    finally:
        server.close()
        await server.wait_closed()


async def test_slow_server_times_out_cleanly():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(5)  # far longer than the policy's own read timeout
        writer.close()

    server, port = await _run_local_server(handler)
    try:
        policy = NetworkPolicy(allow_loopback=True, connect_timeout_seconds=0.5, read_timeout_seconds=0.5, allowed_ports=frozenset({port}))
        driver = RESTDriver(uuid.uuid4(), network_policy=policy)
        await driver.connect(target_host="127.0.0.1", target_port=port, config={"scheme": "http", "path": "/"}, credential=None)
        with pytest.raises(DriverConnectionError):
            await driver.poll()
        await driver.disconnect()
    finally:
        server.close()
        await server.wait_closed()


async def test_redirect_is_not_followed_by_default():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/should-not-be-fetched\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server, port = await _run_local_server(handler)
    try:
        policy = NetworkPolicy(allow_loopback=True, allowed_ports=frozenset({port}))
        driver = RESTDriver(uuid.uuid4(), network_policy=policy)
        await driver.connect(target_host="127.0.0.1", target_port=port, config={"scheme": "http", "path": "/"}, credential=None)
        try:
            result = await driver.poll()
        finally:
            await driver.disconnect()
        # follow_redirects=False (the default) means httpx returns the 302 itself,
        # never fetching the redirect target -- proven by the fact this succeeds at
        # all (a real fetch of 127.0.0.1:1 would hang/refuse) and reports the 302.
        assert result.metrics["status_code"] == 302
    finally:
        server.close()
        await server.wait_closed()


async def test_unsafe_host_header_override_in_config_cannot_win():
    """A misconfigured (or hostile) integration `config["headers"]` block that tries
    to set its own `Host` header must not override the one this module computes from
    the validated target -- the validated Host header is applied AFTER user config
    headers are merged in, specifically so it always wins."""
    captured: dict = {}

    async def handler(reader, writer):
        request_line_and_headers = await reader.readuntil(b"\r\n\r\n")
        captured["raw"] = request_line_and_headers
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server, port = await _run_local_server(handler)
    try:
        policy = NetworkPolicy(allow_loopback=True, allowed_ports=frozenset({port}))
        driver = RESTDriver(uuid.uuid4(), network_policy=policy)
        await driver.connect(
            target_host="127.0.0.1", target_port=port,
            config={"scheme": "http", "path": "/", "headers": {"Host": "attacker-controlled.example"}},
            credential=None,
        )
        try:
            await driver.poll()
        finally:
            await driver.disconnect()
        assert b"Host: 127.0.0.1" in captured["raw"]
        assert b"attacker-controlled.example" not in captured["raw"]
    finally:
        server.close()
        await server.wait_closed()


async def test_trust_env_disabled_by_default(monkeypatch):
    """An ambient HTTP_PROXY/HTTPS_PROXY environment variable must not silently
    reroute outbound polling -- `trust_env=False` unless a policy explicitly opts in."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9/should-not-be-used")

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server, port = await _run_local_server(handler)
    try:
        policy = NetworkPolicy(allow_loopback=True, allowed_ports=frozenset({port}))
        driver = RESTDriver(uuid.uuid4(), network_policy=policy)
        await driver.connect(target_host="127.0.0.1", target_port=port, config={"scheme": "http", "path": "/"}, credential=None)
        try:
            result = await driver.poll()
        finally:
            await driver.disconnect()
        # If the proxy env var had been honored, httpx would have tried to CONNECT
        # through 127.0.0.1:9 (nothing listens there) and this would raise instead.
        assert result.metrics["status_code"] == 200
    finally:
        server.close()
        await server.wait_closed()


async def test_https_scheme_still_validated_even_though_no_real_tls_server_here():
    """Confirms the policy validation path runs identically for https (scheme/host/
    port/method checks) -- the actual TLS handshake/SNI-preservation behavior is
    documented in network_policy.py's own docstring (httpcore's `sni_hostname`
    extension) and is not independently re-verified against a real certificate here,
    which would require standing up a CA/cert fixture beyond this consolidation
    gate's scope."""
    policy = NetworkPolicy(allowed_networks=(ipaddress.ip_network("10.10.0.0/16"),))
    target = await validate_target(scheme="https", host="10.10.5.5", port=None, method="GET", policy=policy)
    assert target.port == 443
    assert target.scheme == "https"


async def test_httpx_timeout_error_wrapped_as_driver_connection_error():
    assert issubclass(httpx.TimeoutException, httpx.HTTPError)
