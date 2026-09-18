"""Genuine driver tests -- ICMP against a real raw socket pinging 127.0.0.1 (not
mocked), REST against this application's own /api/v1/health endpoint via
`httpx.ASGITransport` (not mocked), SNMP against the documented `SimulatedSNMPTransport`
(honestly not a real network test -- see app/application/drivers/snmp.py's own
docstring)."""

import uuid

import httpx
import pytest

from app.application.drivers.base import DriverConnectionError
from app.application.drivers.icmp import ICMPDriver
from app.application.drivers.rest import RESTDriver
from app.application.drivers.snmp import MetricMapping, SimulatedSNMPTransport, SNMPDriver
from app.main import app


@pytest.mark.asyncio
async def test_icmp_driver_pings_localhost_successfully():
    driver = ICMPDriver(uuid.uuid4(), timeout_seconds=3.0)
    await driver.connect(target_host="127.0.0.1", target_port=None, config={}, credential=None)
    try:
        result = await driver.poll()
    finally:
        await driver.disconnect()
    assert result.external_identifier == "127.0.0.1"
    assert result.metrics["reachable"] is True
    assert result.metrics["round_trip_ms"] >= 0


@pytest.mark.asyncio
async def test_icmp_driver_times_out_when_no_reply_arrives_in_time():
    # This sandbox's own network setup answers ICMP echo requests to essentially any
    # address (a transparent proxy/NAT layer, not a real routing decision) -- so RFC
    # 5737's "guaranteed unreachable" TEST-NET-1 address is not a reliable way to
    # provoke a timeout here (confirmed empirically: 192.0.2.1 answered with a matching
    # echo reply in this environment). Loopback ping is also too fast for a
    # sub-millisecond timeout to reliably win the race against the kernel's own
    # loopback delivery -- a timeout of exactly 0 deterministically exercises the same
    # "deadline already passed" branch on the very first loop iteration, before ever
    # calling recv(), regardless of how fast the real reply would have been.
    driver = ICMPDriver(uuid.uuid4(), timeout_seconds=0.0)
    await driver.connect(target_host="127.0.0.1", target_port=None, config={}, credential=None)
    try:
        with pytest.raises(DriverConnectionError):
            await driver.poll()
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_icmp_driver_disconnect_is_idempotent():
    driver = ICMPDriver(uuid.uuid4())
    await driver.disconnect()  # never connected -- must not raise
    await driver.connect(target_host="127.0.0.1", target_port=None, config={}, credential=None)
    await driver.disconnect()
    await driver.disconnect()  # already disconnected -- must not raise


@pytest.mark.asyncio
async def test_rest_driver_polls_real_health_endpoint_in_process():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        driver = RESTDriver(uuid.uuid4(), client=http_client)
        await driver.connect(
            target_host="test", target_port=None,
            config={"scheme": "http", "path": "/api/v1/health/live"}, credential=None,
        )
        try:
            result = await driver.poll()
        finally:
            await driver.disconnect()
        assert result.metrics["status_code"] == 200
        assert result.metrics["reachable"] is True


@pytest.mark.asyncio
async def test_rest_driver_raises_on_http_error_status():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        driver = RESTDriver(uuid.uuid4(), client=http_client)
        await driver.connect(
            target_host="test", target_port=None,
            config={"scheme": "http", "path": "/api/v1/nonexistent-path-xyz"}, credential=None,
        )
        with pytest.raises(DriverConnectionError):
            await driver.poll()


@pytest.mark.asyncio
async def test_snmp_driver_with_simulated_transport():
    mappings = [MetricMapping(oid="1.3.6.1.2.1.1.1.0", metric_name="sys_descr")]
    transport = SimulatedSNMPTransport(oid_values={"1.3.6.1.2.1.1.1.0": "Simulated Device v1"})
    driver = SNMPDriver(uuid.uuid4(), metric_mappings=mappings, transport_factory=lambda *a: transport)
    await driver.connect(target_host="10.0.0.5", target_port=161, config={"version": "v2c"}, credential="public")
    result = await driver.poll()
    assert result.metrics["sys_descr"] == "Simulated Device v1"
    await driver.disconnect()
    assert transport.closed is True


@pytest.mark.asyncio
async def test_snmp_driver_rejects_unsupported_version():
    driver = SNMPDriver(uuid.uuid4(), transport_factory=lambda *a: SimulatedSNMPTransport())
    with pytest.raises(DriverConnectionError):
        await driver.connect(target_host="10.0.0.5", target_port=161, config={"version": "v9-bogus"}, credential=None)


@pytest.mark.asyncio
async def test_snmp_driver_without_transport_factory_is_honest_about_scope():
    """No transport_factory configured (the real production gap this phase discloses
    rather than fakes) -- must fail clearly, never silently return fabricated data."""
    driver = SNMPDriver(uuid.uuid4())
    with pytest.raises(DriverConnectionError):
        await driver.connect(target_host="10.0.0.5", target_port=161, config={}, credential=None)
