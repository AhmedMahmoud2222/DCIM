"""The one place outbound notification and ITSM HTTP calls are made (Issue #103).

Every call is validated by the repository's network-target policy (`drivers/network_policy.py`: deny-by-default
allowlist, cloud-metadata and link-local ranges always blocked, every resolved address checked, the connection
pinned to a validated address so DNS cannot change between check and connect). Redirects are never followed,
ambient proxy variables are ignored, credentials in the URL are refused, and the response body is read in
bounded chunks, so a hostile or broken provider cannot make a worker follow a redirect to an internal address
or buffer an unbounded body.

Errors carry a fixed code and never the provider's text, the URL, or any header."""

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.application.drivers.network_policy import (
    NetworkPolicy,
    NetworkPolicyError,
    build_connect_url,
    httpx_request_kwargs,
    validate_target,
)
from app.core.config import get_settings


class OutboundError(Exception):
    """`code` is one of TARGET_BLOCKED, TIMEOUT, CONNECTION_ERROR, RESPONSE_TOO_LARGE."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OutboundResponse:
    status_code: int
    body: bytes
    retry_after_seconds: int | None


def outbound_policy(methods: frozenset[str]) -> NetworkPolicy:
    settings = get_settings()
    networks = []
    for cidr in settings.outbound_allowed_networks:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue  # a malformed entry is simply not honoured
    extra_ports = frozenset({80}) if settings.outbound_allow_http else frozenset()
    schemes = frozenset({"https", "http"}) if settings.outbound_allow_http else frozenset({"https"})
    return NetworkPolicy(
        allowed_networks=tuple(networks), allowed_schemes=schemes, allowed_methods=methods,
        allowed_ports=frozenset(settings.outbound_allowed_ports) | extra_ports,
        allow_loopback=settings.outbound_allow_loopback, max_response_bytes=settings.outbound_max_response_bytes,
        connect_timeout_seconds=settings.outbound_timeout_seconds, read_timeout_seconds=settings.outbound_timeout_seconds,
        write_timeout_seconds=settings.outbound_timeout_seconds, pool_timeout_seconds=settings.outbound_timeout_seconds,
    )


def validate_url_shape(url: str, *, allow_path: bool) -> str:
    """Syntax checks that need no network. Returns the URL, or raises ValueError with a message that does
    not echo the URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("URL must be absolute http(s).")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials.")
    if parts.fragment:
        raise ValueError("URL must not contain a fragment.")
    if not allow_path and (parts.path not in ("", "/") or parts.query):
        raise ValueError("URL must be an origin without a path or query.")
    if len(url) > 2000:
        raise ValueError("URL is too long.")
    return url


def _retry_after(headers: httpx.Headers) -> int | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0, min(int(raw.strip()), 3600))
    except ValueError:
        return None


async def outbound_request(
    method: str, url: str, *, headers: dict[str, str] | None = None, json_body: object | None = None,
    content: bytes | None = None, auth: tuple[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OutboundResponse:
    policy = outbound_policy(frozenset({method.upper()}))
    parts = urlsplit(url)
    try:
        target = await validate_target(
            scheme=parts.scheme, host=parts.hostname or "", port=parts.port, method=method, policy=policy
        )
    except NetworkPolicyError as exc:
        raise OutboundError("TARGET_BLOCKED") from exc
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    connect_url = build_connect_url(target, path)
    kwargs = httpx_request_kwargs(target)
    request_headers = {**(headers or {}), **kwargs["headers"]}
    timeout = httpx.Timeout(
        connect=policy.connect_timeout_seconds, read=policy.read_timeout_seconds,
        write=policy.write_timeout_seconds, pool=policy.pool_timeout_seconds,
    )
    limit = policy.max_response_bytes
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport,
        ) as client:
            async with client.stream(
                method.upper(), connect_url, headers=request_headers, json=json_body, content=content,
                auth=auth, extensions=kwargs.get("extensions"),
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise OutboundError("RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                return OutboundResponse(response.status_code, b"".join(chunks), _retry_after(response.headers))
    except OutboundError:
        raise
    except httpx.TimeoutException as exc:
        raise OutboundError("TIMEOUT") from exc
    except httpx.HTTPError as exc:
        raise OutboundError("CONNECTION_ERROR") from exc
