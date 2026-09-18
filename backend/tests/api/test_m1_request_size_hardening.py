"""Pre-MVP consolidation hardening: M1 -- bound the total collector request body
size BEFORE it is fully buffered/parsed, not trusting `Content-Length` alone.
Complements `tests/api/test_collectors.py::test_ingest_batch_rejects_oversized_batch`
(which bounds RECORD COUNT after the whole small body already parsed) with a genuine
raw-BYTES cap enforced ahead of any JSON parsing."""

import uuid

from app.api.v1.collectors import MAX_COLLECTOR_REQUEST_BYTES
from app.application.collector_auth import compute_signature
from tests.api._phase8_helpers import register_collector


async def _oversized_raw_request(client, collector, n_bytes: int):
    """Deliberately garbage bytes, not valid JSON -- the size cap must reject this
    before any JSON parsing is ever attempted."""
    raw_body = b"x" * n_bytes
    ts = "1700000000"
    nonce = uuid.uuid4().hex
    sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=ts, nonce=nonce, raw_body=raw_body)
    headers = {
        "X-Collector-Id": collector["id"], "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
        "X-Collector-Signature": sig, "Content-Type": "application/json",
    }
    return await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers=headers)


async def test_oversized_raw_body_rejected_before_json_parsing(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp = await _oversized_raw_request(client, collector, MAX_COLLECTOR_REQUEST_BYTES + 1024)
    assert resp.status_code == 413, resp.text
    assert "traceback" not in resp.text.lower()


async def test_body_exactly_at_the_cap_is_not_rejected_by_size_alone(client, auth_headers):
    """The cap must reject what's OVER the limit, not the limit itself -- this sends a
    body of exactly MAX_COLLECTOR_REQUEST_BYTES bytes of garbage; it must fail for a
    DIFFERENT reason (malformed JSON / bad signature timestamp-window, a normal 401,
    since this uses a stale fixed timestamp), never a 413."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp = await _oversized_raw_request(client, collector, MAX_COLLECTOR_REQUEST_BYTES)
    assert resp.status_code != 413, resp.text


async def test_chunked_transfer_without_content_length_still_bounded(client, auth_headers):
    """A client that never sends `Content-Length` at all (streamed/chunked upload)
    must still be bounded by the actual bytes read, not silently allowed through
    because the pre-check has nothing to compare against."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)

    async def _byte_stream():
        chunk = b"x" * 65536
        total = 0
        limit = MAX_COLLECTOR_REQUEST_BYTES + 65536
        while total < limit:
            yield chunk
            total += len(chunk)

    ts = "1700000000"
    nonce = uuid.uuid4().hex
    # A streamed body's real bytes can't be HMAC-precomputed the normal way (the
    # signature would need the full body up front); this test only needs to prove the
    # SIZE cap fires -- any valid-looking headers are enough since the size check runs
    # before signature verification even gets the complete body to check.
    sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=ts, nonce=nonce, raw_body=b"")
    headers_out = {
        "X-Collector-Id": collector["id"], "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
        "X-Collector-Signature": sig, "Content-Type": "application/json", "Transfer-Encoding": "chunked",
    }
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=_byte_stream(), headers=headers_out)
    assert resp.status_code == 413, resp.text
