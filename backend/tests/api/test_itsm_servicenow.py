"""Issue #103 area D/E/I: idempotent ServiceNow-compatible ticketing against a deterministic fake provider."""

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import itsm_service as its
from app.application.itsm.servicenow import ServiceNowAdapter
from app.application.notification_service import LEASE_SECONDS, MAX_ATTEMPTS, backoff_seconds
from app.core.config import get_settings
from app.core.secrets import encrypt_secret
from app.domain.operations.models import CorrelationIncident, ItsmConnection, ItsmTicket
from tests.api._ops_helpers import NOW

PASSWORD = "sn-integration-password"


class FakeServiceNow:
    """In-memory incident table with switchable misbehaviour. Records every request."""

    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.script: list = []  # outcomes consumed by write requests, in order

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") is None:
            return httpx.Response(401)
        method, path = request.method, urlsplit(str(request.url)).path
        if method == "GET":
            q = parse_qs(urlsplit(str(request.url)).query).get("sysparm_query", [""])[0]
            key = q.split("=", 1)[1]
            found = [t for t in self.tickets.values() if t["correlation_id"] == key]
            return httpx.Response(200, json={"result": [self._view(found[0])] if found else []})
        outcome = self.script.pop(0) if self.script else "ok"
        body = json.loads(request.content or b"{}")
        if method == "POST":
            if outcome == "create_then_timeout":
                self._create(body)
                raise httpx.ReadTimeout("lost the response", request=request)
            if outcome != "ok":
                return self._fail(outcome, request)
            return httpx.Response(201, json={"result": self._view(self._create(body))})
        sys_id = path.rsplit("/", 1)[1]
        if outcome != "ok":
            return self._fail(outcome, request)
        self.tickets[sys_id].update({k: v for k, v in body.items()})
        return httpx.Response(200, json={"result": self._view(self.tickets[sys_id])})

    def _create(self, body):
        sys_id = uuid.uuid4().hex
        n = len(self.tickets) + 1
        self.tickets[sys_id] = {"sys_id": sys_id, "number": f"INC{n:07d}", "state": "1", **body}
        return self.tickets[sys_id]

    @staticmethod
    def _view(t):
        return {"sys_id": t["sys_id"], "number": t["number"], "state": t["state"]}

    @staticmethod
    def _fail(outcome, request):
        if outcome == "timeout":
            raise httpx.ConnectTimeout("t", request=request)
        if outcome == "conn":
            raise httpx.ConnectError("c", request=request)
        if outcome == "garbage":
            return httpx.Response(200, content=b"<html>not json token=secret</html>")
        if outcome == "no_result":
            return httpx.Response(200, json={"error": {"message": "x"}})
        if outcome == "bad_sys_id":
            return httpx.Response(201, json={"result": {"sys_id": "../../etc/passwd", "number": "<script>", "state": "1"}})
        if outcome == "huge":
            return httpx.Response(200, content=b"x" * 500_000)
        if outcome == "redirect":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        if isinstance(outcome, tuple):
            return httpx.Response(outcome[0], headers={"Retry-After": str(outcome[1])})
        return httpx.Response(int(outcome))


@pytest.fixture
def sn(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "outbound_allow_loopback", True)
    monkeypatch.setattr(s, "outbound_allow_http", True)
    monkeypatch.setattr(s, "outbound_max_response_bytes", 65_536)
    return FakeServiceNow()


def factory_for(fake):
    def make(connection, password):
        assert password == PASSWORD
        return ServiceNowAdapter(connection.base_url, connection.username, password, transport=httpx.MockTransport(fake.handler))

    return make


async def make_incident(db, *, site_id=None, confidence="high", cause="UPS-1 tripped") -> CorrelationIncident:
    inc = CorrelationIncident(
        id=uuid.uuid4(), dedup_key=f"k-{uuid.uuid4().hex}", rule="shared_power_cause", cause_type="protection_device",
        cause_ref="x", cause_label=cause, confidence=confidence, rationale="Two alarms downstream.", evidence=[], status="open",
        site_id=site_id, opened_at=NOW, last_member_at=NOW, correlation_id="incident:abc", method_version="1", version=1,
    )
    db.add(inc)
    await db.commit()
    return inc


async def make_connection(db, **kw) -> ItsmConnection:
    c = ItsmConnection(
        id=uuid.uuid4(), name=f"sn-{uuid.uuid4().hex[:6]}", provider="servicenow", base_url=kw.pop("base_url", "http://localhost"),
        username="svc_dcim", secret_ciphertext=encrypt_secret(PASSWORD), enabled=kw.pop("enabled", True),
        auto_create=kw.pop("auto_create", False), version=1, **kw,
    )
    db.add(c)
    await db.commit()
    return c


async def new_ticket(db, **kw):
    inc = await make_incident(db, **kw.pop("incident", {}))
    conn = await make_connection(db, **kw)
    tid, created = await its.ensure_ticket(db, inc, conn, now=NOW)
    await db.commit()
    return inc, conn, tid, created


async def ticket(db, tid) -> ItsmTicket:
    return await db.get(ItsmTicket, tid, populate_existing=True)


@pytest.mark.asyncio
async def test_create_once_records_remote_reference_and_a_second_run_does_nothing(db_session, sn):
    inc, conn, tid, created = await new_ticket(db_session)
    assert created and await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn)) == "synced"
    t = await ticket(db_session, tid)
    assert (t.status, t.pending_op, t.external_state, t.attempts, t.failure_code) == ("synced", "update", "new", 1, None)
    assert t.external_number == "INC0000001" and len(t.external_id) == 32 and t.last_synced_at == NOW
    assert len(sn.tickets) == 1
    remote = next(iter(sn.tickets.values()))
    assert remote["correlation_id"] == t.correlation_key and remote["short_description"] == "[DCIM] UPS-1 tripped"
    assert "high" in remote["description"] and str(inc.id) in remote["description"] and remote["urgency"] == "1"
    before = len(sn.requests)
    assert await its.sync_ticket(db_session, tid, now=NOW + timedelta(hours=1), adapter_factory=factory_for(sn)) == "skipped"
    assert len(sn.requests) == before


@pytest.mark.asyncio
async def test_duplicate_enqueue_for_the_same_incident_makes_one_ticket(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    again, created = await its.ensure_ticket(db_session, inc, conn, now=NOW)
    await db_session.commit()
    assert again == tid and created is False
    assert (await db_session.execute(text("select count(*) from itsm_ticket"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_ambiguous_create_failure_is_retried_without_a_duplicate_ticket(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    sn.script = ["create_then_timeout"]  # the provider created the ticket but the response was lost
    assert await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn)) == "retry"
    t = await ticket(db_session, tid)
    assert t.status == "retry" and t.external_id is None and t.failure_code == "TIMEOUT"
    assert len(sn.tickets) == 1  # exists remotely, unknown locally
    later = NOW + timedelta(seconds=backoff_seconds(1))
    assert await its.sync_ticket(db_session, tid, now=later, adapter_factory=factory_for(sn)) == "synced"
    t = await ticket(db_session, tid)
    assert t.external_id == next(iter(sn.tickets)) and len(sn.tickets) == 1
    posts = [r for r in sn.requests if r.method == "POST"]
    assert len(posts) == 1  # the retry adopted the ticket instead of POSTing again


@pytest.mark.asyncio
async def test_worker_crash_after_provider_accepts_is_recovered_by_lease_expiry(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    claim = await its._claim(db_session, tid, NOW)  # worker A claims and creates remotely, then dies before recording
    assert claim is not None
    sn._create({"correlation_id": (await ticket(db_session, tid)).correlation_key, "short_description": "x", "description": "x"})
    assert await its.sync_ticket(db_session, tid, now=NOW + timedelta(seconds=5), adapter_factory=factory_for(sn)) == "skipped"
    assert await its.sync_ticket(db_session, tid, now=NOW + timedelta(seconds=LEASE_SECONDS + 1), adapter_factory=factory_for(sn)) == "synced"
    assert len(sn.tickets) == 1 and not [r for r in sn.requests if r.method == "POST"]
    assert await its._finish(db_session, tid, claim[0], status="failed", failure_code="TIMEOUT") is False  # stale owner fenced out


@pytest.mark.asyncio
async def test_operator_resolution_updates_the_same_ticket(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    inc = await db_session.get(CorrelationIncident, inc.id)
    inc.status, inc.resolved_at = "resolved", NOW
    await db_session.commit()
    assert await its.request_update(db_session, inc.id, now=NOW) == [tid]
    await db_session.commit()
    assert await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn)) == "synced"
    assert len(sn.tickets) == 1
    patch = [r for r in sn.requests if r.method == "PATCH"]
    assert len(patch) == 1
    body = json.loads(patch[0].content)
    assert "correlation_id" not in body and "resolved" in body["description"]
    # nothing changed: no redundant PATCH
    await its.request_update(db_session, inc.id, now=NOW)
    await db_session.commit()
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    assert len([r for r in sn.requests if r.method == "PATCH"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "status", "code", "final"),
    [
        (["timeout"], "retry", "TIMEOUT", False), (["conn"], "retry", "CONNECTION_ERROR", False),
        ([500], "retry", "HTTP_5XX", False), ([503], "retry", "HTTP_5XX", False), ([(429, 120)], "retry", "RATE_LIMITED", False),
        ([401], "failed", "AUTH_FAILED", True), ([403], "failed", "AUTH_FAILED", True), ([400], "failed", "HTTP_4XX", True),
        ([404], "failed", "HTTP_4XX", True), (["redirect"], "failed", "HTTP_4XX", True),
        (["garbage"], "failed", "MALFORMED_RESPONSE", True), (["no_result"], "failed", "MALFORMED_RESPONSE", True),
        (["bad_sys_id"], "failed", "MALFORMED_RESPONSE", True), (["huge"], "failed", "RESPONSE_TOO_LARGE", True),
    ],
)
async def test_provider_failures_map_to_fixed_codes(db_session, sn, script, status, code, final):
    inc, conn, tid, _ = await new_ticket(db_session)
    sn.script = list(script)
    assert await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn)) == status
    t = await ticket(db_session, tid)
    assert (t.status, t.failure_code) == (status, code)
    if status == "retry":
        extra = 120 if code == "RATE_LIMITED" else 0
        assert t.next_attempt_at == NOW + timedelta(seconds=max(backoff_seconds(1), extra))
    assert t.external_id is None
    blob = json.dumps([dict(r._mapping) for r in (await db_session.execute(text("select to_jsonb(t) as j from itsm_ticket t"))).all()], default=str)
    assert "secret" not in blob and PASSWORD not in blob


@pytest.mark.asyncio
async def test_retries_are_bounded(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    sn.script = [503] * MAX_ATTEMPTS
    now, outcomes = NOW, []
    for _ in range(MAX_ATTEMPTS):
        outcomes.append(await its.sync_ticket(db_session, tid, now=now, adapter_factory=factory_for(sn)))
        now = (await ticket(db_session, tid)).next_attempt_at
    assert outcomes == ["retry"] * (MAX_ATTEMPTS - 1) + ["failed"]
    t = await ticket(db_session, tid)
    assert (t.status, t.failure_code) == ("failed", "ATTEMPTS_EXHAUSTED")
    assert await its.sync_ticket(db_session, tid, now=now + timedelta(days=1), adapter_factory=factory_for(sn)) == "skipped"
    assert await its.requeue_failed_ticket(db_session, tid, now=now) is True
    await db_session.commit()
    assert await its.requeue_failed_ticket(db_session, tid, now=now) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://169.254.169.254", "https://10.0.0.5", "https://192.168.1.1", "https://[fd00::5]"])
async def test_unsafe_targets_are_blocked_and_nothing_is_sent(db_session, monkeypatch, base):
    s = get_settings()
    monkeypatch.setattr(s, "outbound_allow_loopback", False)
    inc, conn, tid, _ = await new_ticket(db_session, base_url=base)

    def never(connection, password):
        return ServiceNowAdapter(connection.base_url, connection.username, password, transport=httpx.MockTransport(lambda r: pytest.fail("connected")))

    assert await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=never) == "failed"
    t = await ticket(db_session, tid)
    assert (t.failure_code, t.status) == ("TARGET_BLOCKED", "failed")


@pytest.mark.asyncio
async def test_redirects_are_not_followed(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    sn.script = ["redirect"]
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    assert all(urlsplit(str(r.url)).hostname != "169.254.169.254" for r in sn.requests)


@pytest.mark.asyncio
async def test_disabled_connection_fails_without_contacting_the_provider(db_session, sn):
    inc, conn, tid, _ = await new_ticket(db_session, enabled=False)
    assert await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn)) == "failed"
    assert (await ticket(db_session, tid)).failure_code == "CONNECTION_DISABLED" and sn.requests == []


@pytest.mark.asyncio
async def test_two_workers_cannot_sync_one_ticket_at_once(db_session, db_engine, sn):
    inc, conn, tid, _ = await new_ticket(db_session)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    gate = asyncio.Event()

    def slow_factory(connection, password):
        adapter = ServiceNowAdapter(connection.base_url, connection.username, password, transport=httpx.MockTransport(sn.handler))
        original = adapter.find

        async def slow_find(key):
            await gate.wait()
            return await original(key)

        adapter.find = slow_find  # type: ignore[method-assign]
        return adapter

    async def worker(fac):
        async with factory() as s:
            return await its.sync_ticket(s, tid, now=NOW, adapter_factory=fac)

    first = asyncio.create_task(worker(slow_factory))
    await asyncio.sleep(0.3)
    second = await worker(factory_for(sn))
    gate.set()
    assert second == "skipped" and await first == "synced" and len(sn.tickets) == 1


@pytest.mark.asyncio
async def test_remote_closure_never_changes_dcim_facts(db_session, sn):
    from sqlalchemy import func

    inc, conn, tid, _ = await new_ticket(db_session)
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    for t in sn.tickets.values():
        t["state"] = "7"  # the ITSM operator closed it
    await its.request_update(db_session, inc.id, now=NOW)
    inc2 = await db_session.get(CorrelationIncident, inc.id)
    inc2.cause_label = "UPS-1 tripped (edited)"
    await db_session.commit()
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    t = await ticket(db_session, tid)
    assert t.external_state == "closed"
    inc3 = await db_session.get(CorrelationIncident, inc.id, populate_existing=True)
    assert (inc3.status, inc3.resolved_at) == ("open", None)
    assert (await db_session.execute(select(func.count()).select_from(text("alarm")))).scalar_one() == 0


def test_no_code_path_reads_remote_state_to_change_dcim(tmp_path):
    import pathlib

    app = pathlib.Path(__file__).resolve().parents[2] / "app"
    for rel in ("application/correlation_service.py", "application/alarm_service.py", "application/collector_service.py"):
        assert "external_state" not in (app / rel).read_text(), rel
    api_src = (app / "api/v1").glob("*.py")
    for f in api_src:
        text_ = f.read_text()
        if "external_state" in text_:
            assert "incident.status" not in text_ or f.name == "operations.py"


@pytest.mark.asyncio
async def test_ticket_payload_carries_only_the_incidents_own_data(db_session, sn):
    site_a, site_b = None, None
    inc_a = await make_incident(db_session, cause="UPS-A tripped")
    inc_b = await make_incident(db_session, cause="SECRET-SITE-B-ROOM-X")
    conn = await make_connection(db_session)
    tid, _ = await its.ensure_ticket(db_session, inc_a, conn, now=NOW)
    await db_session.commit()
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    sent = json.dumps(list(sn.tickets.values()))
    assert "UPS-A" in sent and "SECRET-SITE-B-ROOM-X" not in sent and str(inc_b.id) not in sent
    assert site_a is None and site_b is None


@pytest.mark.asyncio
async def test_auto_create_honours_site_and_confidence(db_session):
    site_ok, site_other = uuid.uuid4(), uuid.uuid4()
    from tests.api._phase3_helpers import create_room_and_site  # noqa: F401 - real sites are needed for the FK below

    inc = await make_incident(db_session, confidence="low")
    off = await make_connection(db_session, auto_create=False)
    high_only = await make_connection(db_session, auto_create=True, min_confidence="high")
    anyc = await make_connection(db_session, auto_create=True)
    disabled = await make_connection(db_session, auto_create=True, enabled=False)
    ids = await its.queue_for_incident(db_session, inc, now=NOW)
    await db_session.commit()
    rows = (await db_session.execute(select(ItsmTicket.connection_id))).scalars().all()
    assert set(rows) == {anyc.id} and len(ids) == 1
    assert off.id not in rows and high_only.id not in rows and disabled.id not in rows
    assert site_ok != site_other


@pytest.mark.asyncio
async def test_logs_and_adapter_repr_never_contain_credentials(db_session, sn, caplog):
    caplog.set_level(logging.DEBUG)
    inc, conn, tid, _ = await new_ticket(db_session)
    sn.script = [503]
    await its.sync_ticket(db_session, tid, now=NOW, adapter_factory=factory_for(sn))
    adapter = ServiceNowAdapter("https://x.example", "svc", PASSWORD)
    assert PASSWORD not in repr(adapter) and PASSWORD not in caplog.text and "svc_dcim:" not in caplog.text
