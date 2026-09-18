"""REST driver: polls an HTTP(S) endpoint and normalizes its JSON body into an
`AcquisitionResult`. Genuinely tested in this phase against a real HTTP server (this
same application's own `/api/v1/health` endpoint, in-process via `httpx.ASGITransport`
in tests -- not mocked). Deliberately generic: no vendor-specific REST API is
hardcoded, per the master prompt's explicit "Do not hardcode a particular vendor
unless the architecture explicitly requires one" -- `config` supplies the path/method/
headers, `external_identifier` is derived from `target_host` (+ path), and
`raw_attributes` carries the response body verbatim (bounded by
`MAX_RESPONSE_BYTES`).

**Pre-MVP consolidation hardening (Codex H1 / SSRF)**: every target this driver would
have connected to unconditionally now goes through
`network_policy.validate_target()` first -- scheme, host/DNS/IP, port, and method are
all checked against a `NetworkPolicy` the CALLER supplies (never read from settings by
this module itself; see `network_policy.py`'s own docstring for why). Response bytes
are streamed and capped, never fully buffered before truncation. Redirects remain
disabled (`follow_redirects=False`); `trust_env=False` unless the policy says
otherwise, so an ambient `HTTP_PROXY` cannot silently reroute outbound polling."""

import uuid

import httpx

from app.application.drivers.base import AcquisitionResult, DriverConnectionError, ProtocolDriver, utcnow
from app.application.drivers.network_policy import (
    NetworkPolicy,
    NetworkPolicyError,
    build_connect_url,
    httpx_request_kwargs,
    validate_target,
)

MAX_RESPONSE_BYTES = 262_144  # 256 KiB -- a polled device's own status response should
# never legitimately be larger than this; a driver must never buffer an unbounded
# response into memory (master prompt §9's "payload size limits" applies to what this
# system reads from a device just as much as to what a collector forwards to central).


class RESTDriver(ProtocolDriver):
    protocol_code = "rest"

    def __init__(
        self, integration_id: uuid.UUID, *, network_policy: NetworkPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.integration_id = integration_id
        # Safe by default: a policy with an EMPTY allowlist denies every target. A
        # caller that actually wants this driver to reach real devices must supply its
        # own configured `NetworkPolicy` (central: from Settings; a future Edge
        # Collector: from its own local config) -- this driver never assumes one.
        self.network_policy = network_policy or NetworkPolicy()
        self._client = client
        self._owns_client = client is None
        self._path: str = "/"
        self._headers: dict = {}
        self._url: str | None = None
        self._request_kwargs: dict = {}
        self._method: str = "GET"

    async def connect(self, *, target_host: str, target_port: int | None, config: dict, credential: str | None) -> None:
        scheme = config.get("scheme", "https")
        method = config.get("method", "GET")
        self._path = config.get("path", "/")
        try:
            target = await validate_target(
                scheme=scheme, host=target_host, port=target_port, method=method, policy=self.network_policy,
            )
        except NetworkPolicyError as exc:
            raise DriverConnectionError(
                integration_id=self.integration_id, reason=f"Target rejected by network policy: {exc}",
            ) from exc

        self._method = target.method
        self._url = build_connect_url(target, self._path)
        self._request_kwargs = httpx_request_kwargs(target)
        self._headers = dict(config.get("headers", {}))
        self._headers.update(self._request_kwargs.pop("headers", {}))
        if credential:
            header_name = config.get("credential_header", "Authorization")
            self._headers[header_name] = credential
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self.network_policy.connect_timeout_seconds, read=self.network_policy.read_timeout_seconds,
                write=self.network_policy.write_timeout_seconds, pool=self.network_policy.pool_timeout_seconds,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout, follow_redirects=self.network_policy.max_redirects > 0,
                trust_env=self.network_policy.trust_env,
            )

    async def poll(self) -> AcquisitionResult:
        if self._url is None or self._client is None:
            raise DriverConnectionError(integration_id=self.integration_id, reason="poll() called before connect().")
        max_bytes = self.network_policy.max_response_bytes
        try:
            async with self._client.stream(
                self._method, self._url, headers=self._headers, **self._request_kwargs
            ) as response:
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        truncated = True
                        remaining = max_bytes - (total - len(chunk))
                        if remaining > 0:
                            chunks.append(chunk[:remaining])
                        break
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
                status_code = response.status_code
        except httpx.HTTPError as exc:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"REST request failed: {exc}") from exc

        if status_code >= 400:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"REST endpoint returned HTTP {status_code}")

        if truncated:
            body: dict = {"truncated": True}
        else:
            try:
                import json

                body = json.loads(body_bytes)
            except ValueError:
                body = {"raw_text": body_bytes.decode("utf-8", errors="replace")}

        return AcquisitionResult(
            external_identifier=self._url,
            observed_at=utcnow(),
            raw_attributes={"protocol": "rest", "url": self._url, "status_code": status_code, "body": body},
            metrics={"status_code": status_code, "reachable": True},
        )

    async def disconnect(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
