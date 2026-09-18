"""REST driver: polls an HTTP(S) endpoint and normalizes its JSON body into an
`AcquisitionResult`. Genuinely tested in this phase against a real HTTP server (this
same application's own `/api/v1/health` endpoint, in-process via `httpx.ASGITransport`
in tests -- not mocked). Deliberately generic: no vendor-specific REST API is
hardcoded, per the master prompt's explicit "Do not hardcode a particular vendor
unless the architecture explicitly requires one" -- `config` supplies the path/method/
headers, `external_identifier` is derived from `target_host` (+ path), and
`raw_attributes` carries the response body verbatim (bounded by
`MAX_RESPONSE_BYTES`)."""

import uuid

import httpx

from app.application.drivers.base import AcquisitionResult, DriverConnectionError, ProtocolDriver, utcnow

_DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 262_144  # 256 KiB -- a polled device's own status response should
# never legitimately be larger than this; a driver must never buffer an unbounded
# response into memory (master prompt §9's "payload size limits" applies to what this
# system reads from a device just as much as to what a collector forwards to central).


class RESTDriver(ProtocolDriver):
    protocol_code = "rest"

    def __init__(
        self, integration_id: uuid.UUID, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.integration_id = integration_id
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._url: str | None = None
        self._method: str = "GET"
        self._headers: dict = {}

    async def connect(self, *, target_host: str, target_port: int | None, config: dict, credential: str | None) -> None:
        scheme = config.get("scheme", "https")
        path = config.get("path", "/")
        port_part = f":{target_port}" if target_port else ""
        self._url = f"{scheme}://{target_host}{port_part}{path}"
        self._method = config.get("method", "GET").upper()
        self._headers = dict(config.get("headers", {}))
        if credential:
            header_name = config.get("credential_header", "Authorization")
            self._headers[header_name] = credential
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)

    async def poll(self) -> AcquisitionResult:
        if self._url is None or self._client is None:
            raise DriverConnectionError(integration_id=self.integration_id, reason="poll() called before connect().")
        try:
            response = await self._client.request(self._method, self._url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"REST request failed: {exc}") from exc

        if response.status_code >= 400:
            raise DriverConnectionError(
                integration_id=self.integration_id, reason=f"REST endpoint returned HTTP {response.status_code}"
            )

        body_bytes = response.content[:MAX_RESPONSE_BYTES]
        try:
            body = response.json() if len(response.content) <= MAX_RESPONSE_BYTES else {"truncated": True}
        except ValueError:
            body = {"raw_text": body_bytes.decode("utf-8", errors="replace")}

        return AcquisitionResult(
            external_identifier=self._url,
            observed_at=utcnow(),
            raw_attributes={"protocol": "rest", "url": self._url, "status_code": response.status_code, "body": body},
            metrics={"status_code": response.status_code, "reachable": True},
        )

    async def disconnect(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
