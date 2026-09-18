from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .queue import QueueRecord


class RetryableTransportError(RuntimeError):
    """A request did not produce a complete, trustworthy central acknowledgement."""


class MalformedResponseError(RetryableTransportError):
    """Central returned a response that cannot safely drive local deletion."""


@dataclass(frozen=True, slots=True)
class AckResult:
    acknowledged_ids: frozenset[str]
    unacknowledged_ids: frozenset[str]


class CentralClient:
    """Outbound-only HMAC client for Central's collector ingress endpoints."""

    def __init__(
        self,
        base_url: str,
        collector_id: uuid.UUID,
        secret: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.collector_id = collector_id
        self._secret = secret
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport)

    def close(self) -> None:
        self._client.close()

    def flush(self, records: Sequence[QueueRecord]) -> AckResult:
        if not records:
            return AckResult(frozenset(), frozenset())
        batch_id = uuid.uuid4().hex
        record_ids = {record.record_id for record in records}
        if len(record_ids) != len(records):
            raise ValueError("batch records must have unique record IDs")
        body = {
            "batch_id": batch_id,
            "records": [self._ingest_record(record) for record in records],
        }
        response = self._post(f"/collectors/{self.collector_id}/ingest", body)
        try:
            response_body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MalformedResponseError("central response was not JSON") from error
        return self._parse_ack(response_body, batch_id=batch_id, record_ids=record_ids)

    def heartbeat(self, *, queue_depth: int, status: str = "ok") -> None:
        response = self._post(
            f"/collectors/{self.collector_id}/heartbeat",
            {"queue_depth": queue_depth, "status": status},
        )
        if response.status_code != 204:
            raise RetryableTransportError(f"unexpected heartbeat status {response.status_code}")

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        raw_body = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        message = f"{self.collector_id}.{timestamp}.{nonce}.".encode() + raw_body
        signature = hmac.new(self._secret.encode(), message, hashlib.sha256).hexdigest()
        try:
            response = self._client.post(
                path,
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Collector-Id": str(self.collector_id),
                    "X-Collector-Timestamp": timestamp,
                    "X-Collector-Nonce": nonce,
                    "X-Collector-Signature": signature,
                },
            )
        except httpx.HTTPError as error:
            raise RetryableTransportError("central transport unavailable") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableTransportError(f"central returned retryable status {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise RetryableTransportError(f"central returned unexpected status {response.status_code}")
        return response

    @staticmethod
    def _ingest_record(record: QueueRecord) -> dict[str, Any]:
        payload = record.payload
        try:
            return {
                "dedup_key": record.record_id,
                "integration_id": payload["integration_id"],
                "external_identifier": payload["external_identifier"],
                "occurred_at": record.occurred_at.isoformat(),
                "raw_attributes": payload.get("raw_attributes", {}),
            }
        except KeyError as error:
            raise ValueError(f"queue record missing central ingest field: {error.args[0]}") from error

    @staticmethod
    def _parse_ack(response_body: Any, *, batch_id: str, record_ids: set[str]) -> AckResult:
        if not isinstance(response_body, dict) or response_body.get("batch_id") != batch_id:
            raise MalformedResponseError("central response did not acknowledge this batch")
        results = response_body.get("results")
        if not isinstance(results, list) or len(results) != len(record_ids):
            raise MalformedResponseError("central response did not acknowledge every record")
        acknowledged: set[str] = set()
        returned_ids: set[str] = set()
        for result in results:
            if not isinstance(result, dict):
                raise MalformedResponseError("central response has an invalid record result")
            record_id = result.get("dedup_key")
            status = result.get("status")
            if not isinstance(record_id, str) or record_id not in record_ids or record_id in returned_ids:
                raise MalformedResponseError("central response has invalid record identifiers")
            if status not in {"accepted", "duplicate", "rejected"}:
                raise MalformedResponseError("central response has an unknown acknowledgement status")
            returned_ids.add(record_id)
            if status in {"accepted", "duplicate"}:
                acknowledged.add(record_id)
        if returned_ids != record_ids:
            raise MalformedResponseError("central response omitted a record acknowledgement")
        return AckResult(frozenset(acknowledged), frozenset(record_ids - acknowledged))
