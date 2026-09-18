"""Unit-level tests for app/application/collector_auth.py -- the machine-to-machine
trust boundary (master prompt §9). Genuine HMAC verification against a real Fernet-
encrypted secret in a real database, not mocked."""

import time
import uuid
from datetime import UTC, datetime

import pytest

from app.application.collector_auth import CollectorAuthError, compute_signature, verify_collector_request
from app.core.secrets import encrypt_secret
from app.domain.integration.models import Collector


async def _make_collector(db_session, *, status="active", secret="s3cr3t-plaintext") -> tuple[Collector, str]:
    collector = Collector(
        id=uuid.uuid4(), name=f"c-{uuid.uuid4().hex[:8]}", collector_type="central", site_id=None,
        status=status, secret_ciphertext=encrypt_secret(secret), secret_rotated_at=datetime.now(UTC),
    )
    db_session.add(collector)
    await db_session.flush()
    return collector, secret


@pytest.mark.asyncio
async def test_valid_signature_authenticates(db_session):
    collector, secret = await _make_collector(db_session)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body = b'{"queue_depth": 0}'
    sig = compute_signature(secret=secret, collector_id=collector.id, timestamp=ts, nonce=nonce, raw_body=body)

    result = await verify_collector_request(
        db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
        signature_header=sig, raw_body=body,
    )
    assert result.id == collector.id


@pytest.mark.asyncio
async def test_tampered_body_rejected(db_session):
    collector, secret = await _make_collector(db_session)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    sig = compute_signature(secret=secret, collector_id=collector.id, timestamp=ts, nonce=nonce, raw_body=b'{"a":1}')

    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
            signature_header=sig, raw_body=b'{"a":2}',  # body changed after signing
        )


@pytest.mark.asyncio
async def test_wrong_secret_rejected(db_session):
    collector, _secret = await _make_collector(db_session)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body = b"{}"
    sig = compute_signature(secret="wrong-secret", collector_id=collector.id, timestamp=ts, nonce=nonce, raw_body=body)

    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
            signature_header=sig, raw_body=body,
        )


@pytest.mark.asyncio
async def test_expired_timestamp_rejected(db_session):
    collector, secret = await _make_collector(db_session)
    old_ts = str(int(time.time()) - 10_000)  # far outside the 300s window
    nonce = uuid.uuid4().hex
    body = b"{}"
    sig = compute_signature(secret=secret, collector_id=collector.id, timestamp=old_ts, nonce=nonce, raw_body=body)

    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(collector.id), timestamp_header=old_ts, nonce_header=nonce,
            signature_header=sig, raw_body=body,
        )


@pytest.mark.asyncio
async def test_replayed_nonce_rejected_on_second_use(db_session):
    collector, secret = await _make_collector(db_session)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body = b"{}"
    sig = compute_signature(secret=secret, collector_id=collector.id, timestamp=ts, nonce=nonce, raw_body=body)

    # First use succeeds.
    await verify_collector_request(
        db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
        signature_header=sig, raw_body=body,
    )
    # Exact replay (same signature, same nonce) must be rejected.
    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
            signature_header=sig, raw_body=body,
        )


@pytest.mark.asyncio
async def test_disabled_collector_rejected(db_session):
    collector, secret = await _make_collector(db_session, status="disabled")
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body = b"{}"
    sig = compute_signature(secret=secret, collector_id=collector.id, timestamp=ts, nonce=nonce, raw_body=body)

    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(collector.id), timestamp_header=ts, nonce_header=nonce,
            signature_header=sig, raw_body=body,
        )


@pytest.mark.asyncio
async def test_unknown_collector_rejected(db_session):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body = b"{}"
    fake_id = uuid.uuid4()
    sig = compute_signature(secret="whatever", collector_id=fake_id, timestamp=ts, nonce=nonce, raw_body=body)

    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header=str(fake_id), timestamp_header=ts, nonce_header=nonce,
            signature_header=sig, raw_body=body,
        )


@pytest.mark.asyncio
async def test_malformed_collector_id_rejected(db_session):
    with pytest.raises(CollectorAuthError):
        await verify_collector_request(
            db_session, collector_id_header="not-a-uuid", timestamp_header=str(int(time.time())),
            nonce_header=uuid.uuid4().hex, signature_header="a" * 64, raw_body=b"{}",
        )
