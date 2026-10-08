"""Issue #103 area C: durable, deduplicated, fenced, bounded-retry notification delivery."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import notification_service as ns
from app.application.outbound_http import OutboundError, OutboundResponse
from app.core.config import get_settings
from app.core.secrets import encrypt_secret
from app.domain.operations.models import NotificationChannel, NotificationDelivery, NotificationPolicy

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
URL_SECRET = "https://hooks.example/services/T000/B000/super-secret-path-token"
SIGN_SECRET = "signing-secret-value"


async def make_delivery(db, *, enabled=True, attempts=0, max_attempts=5, status="pending", next_at=NOW) -> NotificationDelivery:
    ch = NotificationChannel(
        id=uuid.uuid4(), name=f"ch-{uuid.uuid4().hex[:6]}", kind="webhook", url_display="https://hooks.example",
        url_ciphertext=encrypt_secret(URL_SECRET), secret_ciphertext=encrypt_secret(SIGN_SECRET), enabled=enabled, version=1,
    )
    db.add(ch)
    await db.flush()
    pol = NotificationPolicy(
        id=uuid.uuid4(), name=f"p-{uuid.uuid4().hex[:6]}", channel_id=ch.id, event_types=["incident.opened"], enabled=True, version=1
    )
    db.add(pol)
    await db.flush()
    d = NotificationDelivery(
        id=uuid.uuid4(), policy_id=pol.id, channel_id=ch.id, dedup_key=f"k-{uuid.uuid4().hex}", event_type="incident.opened",
        source_type="incident", source_id=uuid.uuid4(), correlation_id="corr-1", status=status, attempts=attempts,
        max_attempts=max_attempts, next_attempt_at=next_at, claim_generation=0, final_failure=False,
        payload={"event": "incident.opened", "probable_cause": "UPS-1"},
    )
    db.add(d)
    await db.commit()
    return d


def sender_returning(*outcomes):
    """Each call pops one outcome: an int status, an (int, retry_after) pair or an exception."""
    calls = []
    queue = list(outcomes)

    async def send(url, body, headers):
        calls.append((url, body, headers))
        o = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(o, Exception):
            raise o
        status, retry = o if isinstance(o, tuple) else (o, None)
        return OutboundResponse(status, b"provider says: token=abc123", retry)

    send.calls = calls
    return send


async def row(db, d):
    return await db.get(NotificationDelivery, d.id, populate_existing=True)


@pytest.mark.asyncio
async def test_success_signs_the_body_and_records_sent(db_session):
    d = await make_delivery(db_session)
    send = sender_returning(204)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=send) == "sent"
    r = await row(db_session, d)
    assert (r.status, r.attempts, r.last_http_status, r.failure_code, r.final_failure) == ("sent", 1, 204, None, False)
    assert r.sent_at == NOW and r.lease_expires_at is None
    url, body, headers = send.calls[0]
    assert url == URL_SECRET and json.loads(body)["probable_cause"] == "UPS-1"
    assert headers["X-DCIM-Delivery-Id"] == str(d.id) and headers["X-DCIM-Correlation-Id"] == "corr-1"
    assert headers["X-DCIM-Signature"] == "sha256=" + ns.sign_body(SIGN_SECRET, body, headers["X-DCIM-Timestamp"])
    assert await ns.deliver(db_session, d.id, now=NOW, sender=send) == "skipped"  # already sent: no second send
    assert len(send.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (500, "HTTP_5XX"), (503, "HTTP_5XX"), (408, "HTTP_5XX"), ((429, 90), "RATE_LIMITED"),
        (OutboundError("TIMEOUT"), "TIMEOUT"), (OutboundError("CONNECTION_ERROR"), "CONNECTION_ERROR"),
        (OutboundError("RESPONSE_TOO_LARGE"), "CONNECTION_ERROR"), (RuntimeError("boom token=abc"), "INTERNAL_ERROR"),
    ],
)
async def test_transient_failures_retry_with_backoff(db_session, outcome, code):
    d = await make_delivery(db_session)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=sender_returning(outcome)) == "retry"
    r = await row(db_session, d)
    assert (r.status, r.failure_code, r.attempts, r.final_failure) == ("retry", code, 1, False)
    expected = max(ns.backoff_seconds(1), 90 if isinstance(outcome, tuple) else 0)
    assert r.next_attempt_at == NOW + timedelta(seconds=expected)
    # not due yet: a second call is a no-op
    assert await ns.deliver(db_session, d.id, now=NOW + timedelta(seconds=expected - 1), sender=sender_returning(200)) == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 302, 301])
async def test_client_errors_and_redirects_fail_immediately(db_session, status):
    d = await make_delivery(db_session)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=sender_returning(status)) == "failed"
    r = await row(db_session, d)
    assert (r.status, r.failure_code, r.final_failure, r.attempts) == ("failed", "HTTP_4XX", True, 1)


@pytest.mark.asyncio
async def test_backoff_grows_and_attempts_are_bounded(db_session):
    assert [ns.backoff_seconds(n) for n in (1, 2, 3, 4, 5, 9, 20)] == [30, 60, 120, 240, 480, 3600, 3600]
    d = await make_delivery(db_session, max_attempts=3)
    now = NOW
    outcomes = []
    for _ in range(3):
        outcomes.append(await ns.deliver(db_session, d.id, now=now, sender=sender_returning(503)))
        r = await row(db_session, d)
        now = r.next_attempt_at
    assert outcomes == ["retry", "retry", "failed"]
    r = await row(db_session, d)
    assert (r.status, r.failure_code, r.attempts, r.final_failure) == ("failed", "ATTEMPTS_EXHAUSTED", 3, True)
    assert await ns.deliver(db_session, d.id, now=now + timedelta(days=1), sender=sender_returning(200)) == "skipped"


@pytest.mark.asyncio
async def test_disabled_channel_fails_without_sending(db_session):
    d = await make_delivery(db_session, enabled=False)
    send = sender_returning(200)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=send) == "failed"
    assert send.calls == [] and (await row(db_session, d)).failure_code == "CHANNEL_DISABLED"


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_one_attempt(db_session, db_engine):
    d = await make_delivery(db_session)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    gate = asyncio.Event()
    sent = []

    async def slow_sender(url, body, headers):
        sent.append(1)
        await gate.wait()
        return OutboundResponse(200, b"", None)

    async def worker():
        async with factory() as s:
            return await ns.deliver(s, d.id, now=NOW, sender=slow_sender)

    t1 = asyncio.create_task(worker())
    await asyncio.sleep(0.3)
    second = await worker()  # claim fails while the first holds a live lease
    gate.set()
    assert second == "skipped" and await t1 == "sent" and len(sent) == 1


@pytest.mark.asyncio
async def test_crashed_worker_is_reclaimed_after_lease_and_stale_owner_cannot_write(db_session):
    d = await make_delivery(db_session)
    first = await ns._claim(db_session, d.id, NOW)  # worker A claims, then "crashes" mid-send
    assert first is not None and first.generation == 1
    assert await ns._claim(db_session, d.id, NOW + timedelta(seconds=ns.LEASE_SECONDS - 1)) is None
    reclaimed = await ns._claim(db_session, d.id, NOW + timedelta(seconds=ns.LEASE_SECONDS + 1))
    assert reclaimed is not None and reclaimed.generation == 2 and reclaimed.attempts == 2
    # the original worker wakes up and tries to record success: fenced out
    assert await ns._finish(db_session, first, status="sent", now=NOW + timedelta(seconds=500)) is False
    r = await row(db_session, d)
    assert r.status == "sending" and r.sent_at is None
    assert await ns._finish(db_session, reclaimed, status="sent", now=NOW + timedelta(seconds=501)) is True
    assert (await row(db_session, d)).status == "sent"


@pytest.mark.asyncio
async def test_operator_requeue_and_automatic_retry_do_not_double_send(db_session):
    d = await make_delivery(db_session, max_attempts=1)
    send = sender_returning(500)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=send) == "failed"
    assert await ns.requeue_failed(db_session, d.id, now=NOW) is True
    await db_session.commit()
    assert await ns.requeue_failed(db_session, d.id, now=NOW) is False  # no longer failed
    ok = sender_returning(200)
    assert await ns.deliver(db_session, d.id, now=NOW, sender=ok) == "sent"
    assert await ns.deliver(db_session, d.id, now=NOW, sender=ok) == "skipped"
    assert len(ok.calls) == 1


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_policy_and_event(db_session):
    d = await make_delivery(db_session)
    pol = await db_session.get(NotificationPolicy, d.policy_id)
    args = {"event_type": "incident.opened", "source_type": "incident", "source_id": uuid.uuid4(), "site_id": None,
            "payload": {"x": 1}, "correlation_id": "c", "now": NOW}
    first = await ns.enqueue_notifications(db_session, **args)
    await db_session.commit()
    again = await ns.enqueue_notifications(db_session, **args)
    await db_session.commit()
    assert len(first) == 1 and again == []
    n = (await db_session.execute(text("select count(*) from notification_delivery where policy_id = :p"), {"p": pol.id})).scalar_one()
    assert n == 2  # the fixture's own row plus exactly one new delivery


@pytest.mark.asyncio
async def test_secrets_never_reach_rows_audit_logs_or_log_output(db_session, caplog):
    d = await make_delivery(db_session)
    caplog.set_level(logging.DEBUG)
    await ns.deliver(db_session, d.id, now=NOW, sender=sender_returning(RuntimeError(f"failed calling {URL_SECRET} key={SIGN_SECRET}")))
    await ns.deliver(db_session, d.id, now=NOW + timedelta(hours=1), sender=sender_returning(200))
    dump = json.dumps(
        [
            dict(r._mapping)
            for r in (await db_session.execute(text("select to_jsonb(n) as j from notification_delivery n"))).all()
        ],
        default=str,
    ) + json.dumps(
        [dict(r._mapping) for r in (await db_session.execute(text("select to_jsonb(a) as j from audit_log a"))).all()], default=str
    )
    for secret in (URL_SECRET, "super-secret-path-token", SIGN_SECRET, "token=abc123"):
        assert secret not in dump
        assert secret not in caplog.text


@pytest.fixture
def loopback_allowed(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "outbound_allow_loopback", True)
    monkeypatch.setattr(s, "outbound_allow_http", True)
    return s


@pytest.mark.asyncio
async def test_real_transport_pins_validated_address_sets_host_and_never_follows_redirects(loopback_allowed):
    from app.application.outbound_http import outbound_request

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    r = await outbound_request("POST", "http://localhost/hook", content=b"{}", transport=httpx.MockTransport(handler))
    assert r.status_code == 302 and len(seen) == 1  # the redirect target was never requested
    # "localhost" resolves to ::1 on some hosts and 127.0.0.1 on others; either way the dialled address is the
    # validated literal and the original name travels in the Host header.
    assert seen[0].url.host in ("127.0.0.1", "::1") and seen[0].headers["host"] == "localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data", "https://10.1.2.3/hook", "https://192.168.1.10/h", "https://[fd00::1]/h",
        "https://0.0.0.0/h", "http://example.invalid:8080/h", "ftp://example.com/h", "https://user:pw@example.com/h",
    ],
)
async def test_unsafe_targets_are_blocked_before_any_connection(url):
    from app.application.outbound_http import outbound_request

    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("a connection was attempted")

    with pytest.raises((OutboundError, ValueError)) as exc:
        await outbound_request("POST", url, content=b"{}", transport=httpx.MockTransport(handler))
    assert "pw" not in str(exc.value)


@pytest.mark.asyncio
async def test_oversized_and_slow_responses_are_bounded(loopback_allowed, monkeypatch):
    from app.application.outbound_http import outbound_request

    monkeypatch.setattr(loopback_allowed, "outbound_max_response_bytes", 2048)
    big = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 10_000))
    with pytest.raises(OutboundError) as exc:
        await outbound_request("POST", "http://localhost/h", content=b"{}", transport=big)
    assert exc.value.code == "RESPONSE_TOO_LARGE"

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(OutboundError) as exc2:
        await outbound_request("POST", "http://localhost/h", content=b"{}", transport=httpx.MockTransport(timeout))
    assert exc2.value.code == "TIMEOUT"


@pytest.mark.asyncio
async def test_blocked_target_is_a_final_failure_with_a_fixed_code(db_session):
    d = await make_delivery(db_session)

    async def blocked(url, body, headers):
        raise OutboundError("TARGET_BLOCKED")

    assert await ns.deliver(db_session, d.id, now=NOW, sender=blocked) == "failed"
    r = await row(db_session, d)
    assert (r.failure_code, r.final_failure, r.attempts) == ("TARGET_BLOCKED", True, 1)


@pytest.mark.asyncio
async def test_due_scan_returns_only_due_or_expired_work(db_session):
    due = await make_delivery(db_session)
    await make_delivery(db_session, status="sent")
    await make_delivery(db_session, status="retry", next_at=NOW + timedelta(minutes=5))
    expired = await make_delivery(db_session, status="sending")
    await db_session.execute(update(NotificationDelivery).where(NotificationDelivery.id == expired.id).values(lease_expires_at=NOW - timedelta(seconds=1)))
    await db_session.commit()
    ids = await ns.due_delivery_ids(db_session, NOW)
    assert set(ids) == {due.id, expired.id}


@pytest.mark.asyncio
async def test_broker_outage_does_not_fail_the_event_path(client, auth_headers, db_session, monkeypatch):
    """Dispatch failure is swallowed: queued rows stay durable for the sweeper."""
    from app.application import collector_health_sweep as sweep
    from app.domain.integration.models import Collector
    from tests.api._phase3_helpers import create_room_and_site
    from tests.api.test_collector_offline_events import make_policy

    _, site = await create_room_and_site(client, auth_headers)
    await make_policy(db_session, site_id=uuid.UUID(site))
    c = Collector(
        id=uuid.uuid4(), name="c-1", collector_type="edge", site_id=uuid.UUID(site), status="active", secret_ciphertext="x",
        secret_rotated_at=NOW, created_at=NOW - timedelta(days=1),
    )
    db_session.add(c)
    await db_session.commit()

    def down(**kw):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.infrastructure.tasks.notifications.deliver_notification.apply_async", down)
    results = await sweep.sweep_collector_states(db_session, now=NOW)
    assert len(results) == 1
    assert ns.dispatch_pending(db_session) == 0  # nothing raised
    assert (await db_session.execute(select(NotificationDelivery.status))).scalars().all() == ["pending"]


def test_ingestion_and_alarm_paths_do_not_depend_on_notification_or_itsm_code():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    guarded = [
        "application/telemetry_service.py", "application/alarm_service.py", "application/collector_auth.py",
        "api/v1/telemetry.py", "api/v1/alarms.py",
    ]
    for rel in guarded:
        text_ = (root / rel).read_text()
        for banned in ("notification_service", "itsm_service", "outbound_http", "tasks.notifications", "tasks.itsm"):
            assert banned not in text_, f"{rel} must not reference {banned}"
