"""DCIM-safe outbound network-target policy (pre-MVP consolidation hardening, Codex
H1 / SSRF). Deliberately stdlib-only -- no `sqlalchemy`, no `app.db`, no
`app.application.rbac`, no `app.core.config` import -- so this module stays exactly as
portable as `app/application/drivers/` itself already is (§9 of the consolidation
task: "The future Edge Collector must still be able to package the protocol drivers
independently. Policy should be passed into the driver as plain configuration/
domain-neutral data."). Whatever process eventually constructs a `RESTDriver` (today,
`collector_service.py`'s central polling orchestrator; tomorrow, a site Edge
Collector) builds a `NetworkPolicy` from ITS OWN configuration source and passes it
in -- this module never reads settings itself.

**Why this exists at all**: a DCIM product must legitimately reach private-network
management interfaces (a UPS's web UI, a PDU's REST API, a BMS gateway, an
environmental sensor, a network device, a management controller) -- so "block all
RFC1918 addresses" is the wrong fix. The right fix is an explicit, centrally
configured allowlist of the ranges THIS deployment's devices actually live in, plus an
always-denied set of destinations that are never a legitimate DCIM polling target
regardless of how broad that allowlist is (cloud metadata services, unspecified/
multicast/broadcast addresses, and -- unless a caller explicitly opts in, e.g. for a
test fixture -- loopback).

**DNS-rebinding approach, documented rather than left implicit**: `validate_target`
resolves the hostname once, validates EVERY resolved address (not just the first --
a multi-answer DNS response could otherwise let an attacker put one policy-valid and
one policy-invalid address in the same response and race which one gets used), and
returns a `ValidatedTarget` that pins the connection to ONE of those already-validated
addresses. `httpx_request_kwargs` builds the actual request against that pinned IP
literal while presenting the original hostname via the `Host` header (correct
virtual-hosting) and via httpx/httpcore's `sni_hostname` request extension (correct
TLS Server Name Indication AND certificate hostname verification -- httpcore's
`_sync/_async` connection code reads `extensions["sni_hostname"]` and passes it as
`ssl.wrap_socket(server_hostname=...)`, confirmed against the installed httpcore
1.0.9). This closes the standard "resolve now, connect later, DNS answer changes in
between" rebinding gap: whatever this process actually opens a TCP connection to is
exactly the address this module already validated, never a fresh resolution."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class NetworkPolicyError(Exception):
    """A target was rejected by policy -- callers should treat this the same as any
    other pre-connection failure (e.g. wrap it into `DriverConnectionError`), never as
    a programming error."""


@dataclass(frozen=True)
class NetworkPolicy:
    """Plain, portable configuration -- no behavior, no imports beyond the stdlib.
    Safe by construction: every field defaults to the MOST restrictive setting, so a
    `NetworkPolicy()` constructed with no arguments at all denies every target (empty
    `allowed_networks`) rather than defaulting open."""

    # The centrally (or edge-locally) configured allowlist of legitimate DCIM device
    # networks for THIS deployment -- e.g. ["10.10.0.0/16", "172.20.10.0/24"]. Never
    # hardcoded here; always supplied by the caller from its own configuration source.
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_methods: frozenset[str] = frozenset({"GET", "HEAD"})
    # None of these are reachable at all unless a device genuinely needs a
    # non-standard management port -- callers configure this explicitly per
    # deployment, not by editing this module.
    allowed_ports: frozenset[int] = frozenset({80, 443})
    # Explicit opt-in only (e.g. a test fixture polling a local HTTP server). This
    # must never be true for a production deployment's default policy.
    allow_loopback: bool = False
    max_response_bytes: int = 262_144
    max_redirects: int = 0  # redirects disabled by default -- see module docstring
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 5.0
    write_timeout_seconds: float = 5.0
    pool_timeout_seconds: float = 5.0
    # httpx's own default (`trust_env=True`) lets an `HTTP_PROXY`/`HTTPS_PROXY`/
    # `NO_PROXY` environment variable silently redirect outbound requests through a
    # proxy neither this policy nor the operator configuring `allowed_networks`
    # necessarily anticipated -- an accidental way to route around every check above.
    # Explicitly false unless a caller opts in.
    trust_env: bool = False


@dataclass(frozen=True)
class ValidatedTarget:
    """The fully validated, ready-to-connect-to result of `validate_target` -- the
    ONE thing a driver may actually open a socket to. `original_host` is preserved for
    `Host`/SNI purposes; `pinned_ip` is what the transport actually dials."""

    scheme: str
    original_host: str
    pinned_ip: IPAddress
    port: int
    method: str


def canonicalize_host(raw_host: str) -> str:
    """Never make a security decision from a raw string prefix/substring check (the
    master prompt's own explicit instruction) -- this only normalizes the host into a
    form `ipaddress`/`socket` can reason about correctly; every actual policy decision
    happens on the parsed/resolved address objects in `validate_target`."""
    host = raw_host.strip()
    if not host:
        raise NetworkPolicyError("Empty host.")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in host):
        raise NetworkPolicyError("Host contains a control character or embedded whitespace.")
    # Bracketed IPv6 literal, e.g. "[::1]" -- the URL-literal form, not the address's
    # own canonical form.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.lower()


def _unwrap_ipv4_mapped(ip: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) must be judged by the SAME
    policy as the plain IPv4 address it represents -- evaluating it only as an IPv6
    literal would let `::ffff:169.254.169.254` slip past an IPv4-address-shaped
    metadata-range check."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _always_denied_reason(ip: IPAddress, *, allow_loopback: bool) -> str | None:
    """Destinations that are never a legitimate DCIM polling target, independent of
    `allowed_networks` -- a safety net that holds even if an operator's allowlist is
    configured too broadly (e.g. an entire `10.0.0.0/8` for a large private WAN)."""
    ip = _unwrap_ipv4_mapped(ip)
    if ip.is_loopback:
        # Handled deliberately and EXCLUSIVELY here -- Python's `ipaddress` module
        # classifies `::1` as both `is_loopback` and `is_reserved`, so this must
        # short-circuit rather than fall through to the `is_reserved` check below,
        # or an explicitly-allowed loopback target would still get denied as
        # "reserved" immediately afterward.
        return None if allow_loopback else "loopback address (not explicitly enabled for this policy)"
    if ip.is_unspecified:
        return "unspecified address (0.0.0.0 / ::)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "IANA-reserved address range"
    if ip.is_link_local:
        # Covers 169.254.0.0/16 (includes the 169.254.169.254 cloud-metadata address
        # used by AWS/GCP/Azure/etc.) and fe80::/10 -- link-local is never a routed
        # DCIM device address and is always denied, not merely allowlist-gated.
        return "link-local address (includes cloud metadata ranges)"
    if isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("255.255.255.255"):
        return "limited broadcast address"
    return None


def _is_within_allowed_networks(ip: IPAddress, policy: NetworkPolicy) -> bool:
    ip = _unwrap_ipv4_mapped(ip)
    for network in policy.allowed_networks:
        if network.version == ip.version and ip in network:
            return True
    return False


async def resolve_host(host: str) -> list[IPAddress]:
    """A literal IP (IPv4, IPv6, or bracketed-IPv6-literal already stripped by
    `canonicalize_host`) is returned as-is, never re-resolved. A hostname is resolved
    via the system resolver -- run off the event loop thread since `getaddrinfo` is a
    blocking call -- returning every unique address either an A or AAAA record named,
    so the caller can validate ALL of them, not just whichever one a client would
    happen to connect to first."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    import asyncio

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
    except OSError as exc:
        raise NetworkPolicyError(f"DNS resolution failed for {host!r}: {exc}") from exc

    seen: dict[str, IPAddress] = {}
    for family, _type, _proto, _canonname, sockaddr in infos:
        addr = str(sockaddr[0])
        if family == socket.AF_INET6:
            addr = addr.split("%", 1)[0]  # strip a zone/scope id (e.g. "fe80::1%eth0")
        ip = ipaddress.ip_address(addr)
        seen[str(ip)] = ip
    if not seen:
        raise NetworkPolicyError(f"DNS resolution for {host!r} returned no usable address.")
    return list(seen.values())


async def validate_target(*, scheme: str, host: str, port: int | None, method: str, policy: NetworkPolicy) -> ValidatedTarget:
    """The one function every caller must route a REST target through before ever
    opening a connection. Raises `NetworkPolicyError` with a specific, safe-to-log
    reason on any violation; returns a `ValidatedTarget` (pinned to one already-
    validated resolved address) on success."""
    scheme_l = scheme.lower()
    if scheme_l not in policy.allowed_schemes:
        raise NetworkPolicyError(f"Scheme {scheme!r} is not permitted (allowed: {sorted(policy.allowed_schemes)}).")

    method_u = method.upper()
    if method_u not in policy.allowed_methods:
        raise NetworkPolicyError(f"HTTP method {method!r} is not permitted (allowed: {sorted(policy.allowed_methods)}).")

    canonical_host = canonicalize_host(host)
    effective_port = port if port is not None else (443 if scheme_l == "https" else 80)
    if effective_port not in policy.allowed_ports:
        raise NetworkPolicyError(f"Port {effective_port} is not permitted (allowed: {sorted(policy.allowed_ports)}).")

    resolved = await resolve_host(canonical_host)
    for ip in resolved:
        denial = _always_denied_reason(ip, allow_loopback=policy.allow_loopback)
        if denial is not None:
            raise NetworkPolicyError(f"{canonical_host!r} resolves to {ip}, which is always denied: {denial}.")
        explicitly_allowed_loopback = policy.allow_loopback and _unwrap_ipv4_mapped(ip).is_loopback
        if not explicitly_allowed_loopback and not _is_within_allowed_networks(ip, policy):
            raise NetworkPolicyError(
                f"{canonical_host!r} resolves to {ip}, which is not within any configured allowed network."
            )

    return ValidatedTarget(
        scheme=scheme_l, original_host=canonical_host, pinned_ip=resolved[0], port=effective_port, method=method_u,
    )


def build_connect_url(target: ValidatedTarget, path: str) -> str:
    """Builds the URL the transport actually dials -- the pinned IP literal, never the
    original hostname (that's what makes this DNS-rebinding-safe: whatever this
    process resolved and validated is exactly what it connects to)."""
    host_literal = f"[{target.pinned_ip}]" if target.pinned_ip.version == 6 else str(target.pinned_ip)
    is_default_port = (target.scheme == "http" and target.port == 80) or (target.scheme == "https" and target.port == 443)
    port_part = "" if is_default_port else f":{target.port}"
    return f"{target.scheme}://{host_literal}{port_part}{path}"


def httpx_request_kwargs(target: ValidatedTarget) -> dict:
    """`Host` header (correct virtual-hosting/vhost routing at the L7 layer) plus the
    `sni_hostname` request extension (correct TLS SNI and certificate-hostname
    verification against the ORIGINAL hostname, even though the underlying TCP/TLS
    connection targets the pinned IP) -- see the module docstring for why this is
    real, supported httpcore behavior, not a guess."""
    kwargs: dict = {"headers": {"Host": target.original_host}}
    if target.scheme == "https":
        kwargs["extensions"] = {"sni_hostname": target.original_host}
    return kwargs
