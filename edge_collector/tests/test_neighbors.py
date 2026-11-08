"""LLDP/CDP adapters: table decoding, sanitization, malformed-row isolation and queue records."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from edge_collector.client import AckResult, CentralClient, MalformedResponseError
from edge_collector.neighbors import (
    NeighborSpecError,
    ProtocolSpec,
    build_queue_records,
    cdp_capabilities,
    clean_text,
    collect_neighbors,
    lldp_capabilities,
    render_lldp_id,
    scan_cdp,
    scan_lldp,
)
from edge_collector.queue import QueueRecord
from edge_collector.snmp import SNMPTimeoutError
from edge_collector.snmp_wire import (
    TAG_INTEGER,
    TAG_OCTET_STRING,
    Varbind,
    WalkResult,
    oid_in_subtree,
    oid_tuple,
)

LLDP_TABLE = "1.0.8802.1.1.2.1.4.1.1"
LLDP_LOCAL = "1.0.8802.1.1.2.1.3.7.1"
LLDP_ADDR = "1.0.8802.1.1.2.1.4.2.1"
CDP_TABLE = "1.3.6.1.4.1.9.9.23.1.2.1.1"
IFX = "1.3.6.1.2.1.31.1.1.1"

PLAN = {
    "neighbor_discovery": {
        "lldp": {
            "enabled": True, "table_oid": LLDP_TABLE,
            "columns": {"chassis_id_subtype": 4, "chassis_id": 5, "port_id_subtype": 6, "port_id": 7, "port_desc": 8,
                        "sys_name": 9, "sys_desc": 10, "sys_cap_supported": 11, "sys_cap_enabled": 12},
            "index_fields": ["time_mark", "local_port_num", "rem_index"],
            "local_port_table_oid": LLDP_LOCAL, "local_port_columns": {"port_id_subtype": 2, "port_id": 3, "port_desc": 4},
            "management_address_table_oid": LLDP_ADDR,
        },
        "cdp": {
            "enabled": True, "table_oid": CDP_TABLE,
            "columns": {"address_type": 3, "address": 4, "version": 5, "device_id": 6, "device_port": 7, "platform": 8,
                        "capabilities": 9, "native_vlan": 11},
            "index_fields": ["if_index", "device_index"],
            "local_port_table_oid": IFX, "local_port_columns": {"if_name": 1, "if_alias": 18},
        },
    },
    "neighbor_behavior": {
        "lldp": {"enabled": True, "local_port_source": "lldp_loc_port_id", "management_address": True},
        "cdp": {"enabled": True, "device_id_normalization": "strip_domain"},
    },
}


def octets(value: bytes | str) -> tuple[int, bytes]:
    return TAG_OCTET_STRING, value.encode() if isinstance(value, str) else value


def integer(value: int) -> tuple[int, bytes]:
    return TAG_INTEGER, value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)


class FakeWalker:
    def __init__(self, mib: dict[str, tuple[int, bytes]], *, fail_on: dict[str, Exception] | None = None, truncate: set[str] = frozenset()):
        self.mib = mib
        self.fail_on = fail_on or {}
        self.truncate = truncate
        self.walked: list[str] = []

    def walk(self, base_oid: str, *, max_rows: int = 4096) -> WalkResult:
        self.walked.append(base_oid)
        if base_oid in self.fail_on:
            raise self.fail_on[base_oid]
        rows = sorted((o for o in self.mib if oid_in_subtree(o, base_oid)), key=oid_tuple)
        return WalkResult(tuple(Varbind(o, *self.mib[o]) for o in rows), base_oid in self.truncate)


def lldp_row(mib, *, time_mark=1000, local=7, index=1, **columns):
    for name, sub_id in (("chassis_id_subtype", 4), ("chassis_id", 5), ("port_id_subtype", 6), ("port_id", 7), ("port_desc", 8),
                         ("sys_name", 9), ("sys_desc", 10), ("sys_cap_enabled", 12)):
        if name in columns:
            mib[f"{LLDP_TABLE}.{sub_id}.{time_mark}.{local}.{index}"] = columns[name]


def cdp_row(mib, *, if_index=3, device_index=1, **columns):
    for name, sub_id in (("address_type", 3), ("address", 4), ("version", 5), ("device_id", 6), ("device_port", 7),
                         ("platform", 8), ("capabilities", 9), ("native_vlan", 11)):
        if name in columns:
            mib[f"{CDP_TABLE}.{sub_id}.{if_index}.{device_index}"] = columns[name]


def standard_lldp_mib():
    mib: dict[str, tuple[int, bytes]] = {}
    lldp_row(
        mib, chassis_id_subtype=integer(4), chassis_id=octets(bytes.fromhex("0050563a1b2c")), port_id_subtype=integer(5),
        port_id=octets("Gi1/0/24"), port_desc=octets("uplink to core"), sys_name=octets("core-sw-1"),
        sys_desc=octets("Vendor OS 1.2"), sys_cap_enabled=octets(bytes([0b00101000])),
    )
    mib[f"{LLDP_LOCAL}.3.7"] = octets("Eth1/7")  # lldpLocPortId (column 3), port 7
    mib[f"{LLDP_LOCAL}.2.7"] = integer(5)
    mib[f"{LLDP_ADDR}.3.1000.7.1.1.4.10.1.2.3"] = integer(2)  # IPv4 address 10.1.2.3 encoded in the index
    return mib


def test_lldp_row_is_decoded_with_local_port_address_and_capabilities():
    scan = scan_lldp(FakeWalker(standard_lldp_mib()), PLAN)
    assert scan.complete and scan.malformed_rows == 0 and scan.failure is None
    (obs,) = scan.observations
    assert obs["protocol"] == "lldp"
    assert obs["local_port"] == {"name": "Eth1/7", "ref": "7"}
    remote = obs["remote"]
    assert remote["chassis_id"] == "00:50:56:3a:1b:2c" and remote["chassis_id_subtype"] == "mac_address"
    assert remote["port_id"] == "Gi1/0/24" and remote["port_id_subtype"] == "interface_name"
    assert remote["system_name"] == "core-sw-1" and remote["port_description"] == "uplink to core"
    assert remote["management_address"] == "10.1.2.3"
    assert obs["capabilities"] == ["bridge", "router"]
    assert obs["raw"]["sys_name"] == "core-sw-1"


def test_lldp_capability_bits_follow_the_mib_layout():
    assert lldp_capabilities(bytes([0b00100000])) == ["bridge"]
    assert lldp_capabilities(bytes([0b00110000])) == ["bridge", "wlan_access_point"]
    assert lldp_capabilities(bytes([0b10001000])) == ["other", "router"]
    assert lldp_capabilities(b"") == []


def test_cdp_capability_bits_follow_the_mib_layout():
    assert cdp_capabilities(bytes([0, 0, 0, 0x09])) == ["router", "switch"]
    assert cdp_capabilities(b"") == [] and cdp_capabilities(b"\x00" * 5) == []


def test_lldp_id_subtypes_render_deterministically():
    names = {4: "mac_address", 5: "network_address", 7: "local"}
    assert render_lldp_id(4, bytes.fromhex("aabbccddeeff"), names) == ("aa:bb:cc:dd:ee:ff", "mac_address")
    assert render_lldp_id(5, bytes([1, 192, 0, 2, 9]), names) == ("192.0.2.9", "network_address")
    assert render_lldp_id(7, b"rack-3", names) == ("rack-3", "local")
    assert render_lldp_id(7, b"\xff\xfe\xfd", names)[0] == "fffefd"  # not printable text -> hex, never mojibake
    assert render_lldp_id(99, b"ab", names) == ("6162", "unknown")
    assert render_lldp_id(4, b"\x01\x02", names)[0] == "0102"  # a 2-byte "MAC" is not guessed


def cdp_mib():
    mib: dict[str, tuple[int, bytes]] = {}
    cdp_row(
        mib, address_type=integer(1), address=octets(bytes([10, 9, 8, 7])), version=octets("IOS 15.2"),
        device_id=octets("dist-sw-2.example.net"), device_port=octets("TenGigabitEthernet1/1"),
        platform=octets("cisco WS-C3850"), capabilities=octets(bytes([0, 0, 0, 0x28])), native_vlan=integer(42),
    )
    mib[f"{IFX}.1.3"] = octets("Te1/0/3")
    return mib


def test_cdp_row_is_decoded_and_domain_stripped_per_profile():
    scan = scan_cdp(FakeWalker(cdp_mib()), PLAN)
    (obs,) = scan.observations
    assert obs["local_port"] == {"name": "Te1/0/3", "ref": "3"}
    remote = obs["remote"]
    assert remote["chassis_id"] == "dist-sw-2" and remote["chassis_id_subtype"] == "cdp_device_id"
    assert remote["port_id"] == "TenGigabitEthernet1/1" and remote["platform"] == "cisco WS-C3850"
    assert remote["management_address"] == "10.9.8.7"
    assert obs["capabilities"] == ["switch", "igmp"] and obs["native_vlan"] == 42
    plan = json.loads(json.dumps(PLAN))
    plan["neighbor_behavior"]["cdp"]["device_id_normalization"] = "none"
    assert scan_cdp(FakeWalker(cdp_mib()), plan).observations[0]["remote"]["chassis_id"] == "dist-sw-2.example.net"


def test_cdp_ip_literal_device_id_is_never_domain_stripped():
    mib = cdp_mib()
    cdp_row(mib, device_id=octets("10.20.30.40"), device_port=octets("Gi0/1"))
    assert scan_cdp(FakeWalker(mib), PLAN).observations[0]["remote"]["chassis_id"] == "10.20.30.40"


def test_malformed_rows_are_skipped_counted_and_never_abort_the_scan():
    mib = standard_lldp_mib()
    lldp_row(mib, local=8, chassis_id=octets(b""), port_id=octets("x"))          # empty chassis id
    lldp_row(mib, local=9, chassis_id=octets("abc"))                              # missing port id
    lldp_row(mib, local=10, chassis_id=integer(5), port_id=octets("x"))           # wrong BER type
    mib[f"{LLDP_TABLE}.5.1000.11"] = octets("short-index")                        # wrong index arity
    mib[f"{LLDP_TABLE}.5.1000.11.1.9.9.9"] = octets("long-index")                 # wrong index arity
    scan = scan_lldp(FakeWalker(mib), PLAN)
    assert len(scan.observations) == 1 and scan.malformed_rows == 5 and scan.complete


def test_device_supplied_text_is_sanitized_and_bounded():
    mib: dict[str, tuple[int, bytes]] = {}
    lldp_row(
        mib, chassis_id_subtype=integer(7), chassis_id=octets("bad\x00chassis\x1b[31m\nname"), port_id_subtype=integer(5),
        port_id=octets(b"P\xff\xfeort"), sys_name=octets("S" * 5000), sys_desc=octets("D" * 5000),
        port_desc=octets(chr(0x202E) + "evil"),
    )
    (obs,) = scan_lldp(FakeWalker(mib), PLAN).observations
    blob = json.dumps(obs)
    assert "\x00" not in blob and "\x1b" not in blob and "\n" not in obs["remote"]["chassis_id"]
    assert len(obs["remote"]["system_name"]) == 255 and len(obs["remote"]["system_description"]) == 512
    assert len(blob) < 6_000
    assert clean_text(b"a\x07b\x7fc") == "abc"


def test_truncated_walk_is_reported_incomplete():
    scan = scan_lldp(FakeWalker(standard_lldp_mib(), truncate={LLDP_TABLE}), PLAN)
    assert scan.complete is False and len(scan.observations) == 1


def test_protocol_failure_is_isolated_and_reports_only_the_error_class():
    walker = FakeWalker({**standard_lldp_mib(), **cdp_mib()}, fail_on={LLDP_TABLE: SNMPTimeoutError("secret detail 10.0.0.1")})
    scans = {scan.protocol: scan for scan in collect_neighbors(walker, PLAN)}
    assert scans["lldp"].failure == "SNMPTimeoutError" and scans["lldp"].observations == () and not scans["lldp"].complete
    assert "secret" not in repr(scans["lldp"]) and "10.0.0.1" not in repr(scans["lldp"])
    assert len(scans["cdp"].observations) == 1 and scans["cdp"].complete


def test_a_parser_bug_in_one_protocol_does_not_stop_the_other(monkeypatch):
    from edge_collector import neighbors

    def boom(*_a, **_k):
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(neighbors, "scan_lldp", boom)
    walker = FakeWalker({**standard_lldp_mib(), **cdp_mib()})
    scans = {scan.protocol: scan for scan in neighbors.collect_neighbors(walker, PLAN)}
    assert scans["lldp"].failure == "RuntimeError" and "internal" not in repr(scans["lldp"])
    assert len(scans["cdp"].observations) == 1


def test_disabled_protocols_are_not_walked():
    plan = json.loads(json.dumps(PLAN))
    plan["neighbor_behavior"]["cdp"]["enabled"] = False
    walker = FakeWalker({**standard_lldp_mib(), **cdp_mib()})
    assert [scan.protocol for scan in collect_neighbors(walker, plan)] == ["lldp"]
    assert CDP_TABLE not in walker.walked


@pytest.mark.parametrize(
    "raw",
    [None, {"enabled": False}, {"enabled": True}, {"enabled": True, "table_oid": "nope", "columns": {"a": 1}},
     {"enabled": True, "table_oid": "1.3.6.1", "columns": {"Bad": 1}}, {"enabled": True, "table_oid": "1.3.6.1", "columns": {"a": 999}},
     {"enabled": True, "table_oid": "1.3.6.1", "columns": {"a": 1}, "index_fields": ["BAD"]},
     {"enabled": True, "table_oid": "1.3.6.1", "columns": {"a": 1}, "local_port_table_oid": "x"}],
)
def test_hostile_or_broken_plan_sections_are_refused(raw):
    with pytest.raises(NeighborSpecError):
        ProtocolSpec.from_plan(raw)
    plan = {"neighbor_discovery": {"lldp": raw}, "neighbor_behavior": {"lldp": {"enabled": True}}}
    scan = collect_neighbors(FakeWalker({}), plan)[0]
    assert scan.failure == "NeighborSpecError" and scan.observations == ()


def test_queue_records_order_marker_after_neighbors_and_stay_small():
    scans = collect_neighbors(FakeWalker({**standard_lldp_mib(), **cdp_mib()}), PLAN)
    started, finished = datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    records = build_queue_records(
        scans, integration_id="int-1", external_identifier="10.0.0.1", scan_id="scan-1", scan_started_at=started,
        scan_finished_at=finished,
    )
    kinds = [r.payload["record_type"] for r in records]
    assert sorted(kinds) == ["neighbor", "neighbor", "neighbor_scan", "neighbor_scan"]
    ordered = sorted(records, key=lambda r: (r.occurred_at, r.record_id))
    assert [r.payload["record_type"] for r in ordered][-2:] == ["neighbor_scan", "neighbor_scan"]
    assert len({r.record_id for r in records}) == 4
    for record in records:
        assert len(json.dumps(record.payload["raw_attributes"])) < 8192
    marker = next(r for r in records if r.payload["record_type"] == "neighbor_scan" and r.payload["raw_attributes"]["protocol"] == "lldp")
    assert marker.payload["raw_attributes"] == {
        "scan_id": "scan-1", "protocol": "lldp", "scan_started_at": started.isoformat(), "complete": True,
        "observed_count": 1, "malformed_rows": 0,
    }
    again = build_queue_records(
        scans, integration_id="int-1", external_identifier="10.0.0.1", scan_id="scan-1", scan_started_at=started,
        scan_finished_at=finished,
    )
    assert [r.record_id for r in again] == [r.record_id for r in records]  # deterministic -> idempotent re-enqueue


def test_failed_protocol_emits_no_marker_so_nothing_is_aged_out():
    walker = FakeWalker({**standard_lldp_mib()}, fail_on={LLDP_TABLE: SNMPTimeoutError("t")})
    scans = collect_neighbors(walker, PLAN)
    records = build_queue_records(
        scans, integration_id="i", external_identifier="h", scan_id="s",
        scan_started_at=datetime(2026, 1, 1, tzinfo=UTC), scan_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    assert [r.payload["record_type"] for r in records if r.payload["raw_attributes"].get("protocol", "cdp") == "lldp"] == []


def test_client_forwards_record_type_and_drops_permanently_invalid_records():
    record = QueueRecord("r1", datetime(2026, 1, 1, tzinfo=UTC), {
        "integration_id": "i", "external_identifier": "h", "record_type": "neighbor", "raw_attributes": {"a": 1}})
    wire = CentralClient._ingest_record(record)
    assert wire["record_type"] == "neighbor" and wire["dedup_key"] == "r1"
    legacy = QueueRecord("r2", datetime(2026, 1, 1, tzinfo=UTC), {"integration_id": "i", "external_identifier": "h"})
    assert "record_type" not in CentralClient._ingest_record(legacy)

    body = {"batch_id": "b", "results": [
        {"dedup_key": "a", "status": "accepted"},
        {"dedup_key": "b", "status": "rejected", "error_code": "INVALID_PAYLOAD"},
        {"dedup_key": "c", "status": "rejected", "error_code": "NOT_ASSIGNED"},
    ]}
    ack = CentralClient._parse_ack(body, batch_id="b", record_ids={"a", "b", "c"})
    assert ack == AckResult(frozenset({"a", "b"}), frozenset({"c"}))  # only the permanent rejection is dropped
    with pytest.raises(MalformedResponseError):
        CentralClient._parse_ack({"batch_id": "b", "results": [{"dedup_key": "a", "status": "weird"}]}, batch_id="b", record_ids={"a"})


# ------------------------------------------------------------- cycle runner / credentials / plan
def _queue(tmp_path):
    from edge_collector.config import CollectorConfig
    from edge_collector.queue import SQLiteQueue

    return SQLiteQueue(CollectorConfig(database_path=tmp_path / "queue.db"))


def _plan_item(integration_id="int-1", plan=PLAN):
    return {"integration_id": integration_id, "target_host": "192.0.2.9", "target_port": 161, "plan": plan}


def test_cycle_enqueues_neighbors_and_isolates_failing_items(tmp_path):
    from edge_collector.discovery_runner import run_discovery_cycle

    queue = _queue(tmp_path)
    good = FakeWalker({**standard_lldp_mib(), **cdp_mib()})

    def factory(item, _target):
        if item["integration_id"] == "boom":
            raise RuntimeError("password=hunter2 should never be surfaced")
        return good

    plan = [_plan_item("boom"), _plan_item("int-1"), {"integration_id": "no-profile", "target_host": "h", "plan": None}]
    result = run_discovery_cycle(plan, factory, queue)
    by_id = {item.integration_id: item for item in result.items}
    assert by_id["boom"].failures == ["RuntimeError"] and by_id["boom"].enqueued == 0
    assert by_id["no-profile"].failures == ["NoProfilePlan"]
    assert by_id["int-1"].failures == [] and by_id["int-1"].observed == 2 and by_id["int-1"].enqueued == 4  # 2 neighbors + 2 markers
    assert result.failed == 2
    assert "hunter2" not in repr(result)
    assert queue.metrics().count == 4
    again = run_discovery_cycle(plan[1:2], factory, queue)  # a new scan has a new scan id, so records are new
    assert again.items[0].enqueued == 4


def test_cycle_reports_protocol_failures_without_dropping_the_other_protocol(tmp_path):
    from edge_collector.discovery_runner import run_discovery_cycle

    queue = _queue(tmp_path)
    walker = FakeWalker({**standard_lldp_mib(), **cdp_mib()}, fail_on={CDP_TABLE: SNMPTimeoutError("t")})
    result = run_discovery_cycle([_plan_item()], lambda *_: walker, queue)
    (item,) = result.items
    assert item.failures == ["cdp:SNMPTimeoutError"] and item.observed == 1 and item.enqueued == 2  # lldp neighbor + marker


def test_local_credential_store_requires_private_file_and_never_leaks(tmp_path):
    import os

    from edge_collector.credentials import CredentialStoreError, LocalCredentialStore
    from edge_collector.snmp import SNMPTarget, SNMPTargetPolicy
    from edge_collector.snmp_v2c import SNMPv2cSession
    from edge_collector.snmp_v3 import SNMPv3Session

    path = tmp_path / "creds.json"
    path.write_text(json.dumps({
        "v3": {"snmpv3": {"username": "poller", "auth_protocol": "sha256", "auth_secret": "local-auth-secret-1",
                          "priv_protocol": "aes128", "priv_secret": "local-priv-secret-2"}},
        "v2": {"community": "local-community"},
        "bad": {"snmpv3": {"username": "poller", "auth_protocol": "md5", "auth_secret": "local-auth-secret-1",
                           "priv_protocol": "des", "priv_secret": "local-priv-secret-2"}},
        "empty": {},
    }))
    os.chmod(path, 0o644)
    with pytest.raises(CredentialStoreError, match="chmod 600"):
        LocalCredentialStore.load(path)
    os.chmod(path, 0o600)
    store = LocalCredentialStore.load(path)
    policy, target = SNMPTargetPolicy(), SNMPTarget("192.0.2.1")
    assert isinstance(store.session_for("v3", target, policy=policy), SNMPv3Session)
    assert isinstance(store.session_for("v2", target, policy=policy), SNMPv2cSession)
    for missing in ("bad", "empty", "unknown"):
        with pytest.raises(CredentialStoreError) as error:
            store.session_for(missing, target, policy=policy)
        assert "local-" not in str(error.value)
    assert "local-" not in repr(store)
    path.write_text("not json")
    os.chmod(path, 0o600)
    with pytest.raises(CredentialStoreError):
        LocalCredentialStore.load(path)
    with pytest.raises(CredentialStoreError):
        LocalCredentialStore.load(tmp_path / "missing.json")


def test_client_fetches_and_validates_the_discovery_plan():
    import hashlib
    import hmac
    import uuid

    import httpx

    collector_id = uuid.uuid4()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["body"], seen["headers"] = request.method, request.content, dict(request.headers)
        return httpx.Response(200, json=[{"integration_id": "i", "plan": None}])

    client = CentralClient("http://central.test/api/v1", collector_id, "collector-secret", transport=httpx.MockTransport(handler))
    assert client.get_discovery_plan() == [{"integration_id": "i", "plan": None}]
    assert seen["method"] == "GET" and seen["body"] == b"" and "content-type" not in seen["headers"]
    message = f"{collector_id}.{seen['headers']['x-collector-timestamp']}.{seen['headers']['x-collector-nonce']}.".encode()
    assert seen["headers"]["x-collector-signature"] == hmac.new(b"collector-secret", message, hashlib.sha256).hexdigest()

    for payload in ({"not": "a list"}, [1, 2], "text"):
        bad = CentralClient("http://central.test", collector_id, "s", transport=httpx.MockTransport(lambda r, p=payload: httpx.Response(200, json=p)))
        with pytest.raises(MalformedResponseError):
            bad.get_discovery_plan()


# --------------------------------------------- hostile peers cannot make an oversize record
def _wire_size(records):
    return [len(json.dumps(r.payload["raw_attributes"])) for r in records if r.payload["record_type"] == "neighbor"]


def _queue_records(scans):
    started, finished = datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    return build_queue_records(
        scans, integration_id="int-1", external_identifier="10.0.0.1", scan_id="scan-1", scan_started_at=started,
        scan_finished_at=finished,
    )


def test_hostile_lldp_row_never_exceeds_the_central_serialized_limit():
    mib: dict[str, tuple[int, bytes]] = {}
    hostile_text = ("\U0001f600" * 255).encode()  # each astral character escapes to 12 bytes under ensure_ascii
    lldp_row(
        mib, chassis_id_subtype=integer(7), chassis_id=octets("c" * 200), port_id_subtype=integer(7), port_id=octets("p" * 200),
        port_desc=octets(hostile_text), sys_name=octets(hostile_text), sys_desc=octets(hostile_text * 2),
        sys_cap_enabled=octets(bytes([0b00101000])),
    )
    mib[f"{LLDP_TABLE}.9.1000.7.2"] = octets(b"\xff" * 255)
    scans = collect_neighbors(FakeWalker(mib), PLAN)
    assert scans[0].observations, "the hostile row is still a neighbor"
    records = _queue_records(scans)
    assert _wire_size(records) and max(_wire_size(records)) <= 7_000
    marker = next(r for r in records if r.payload["record_type"] == "neighbor_scan")
    assert marker.payload["raw_attributes"]["complete"] is True  # nothing was dropped, only evidence trimmed


def test_hostile_cdp_row_never_exceeds_the_central_serialized_limit():
    mib: dict[str, tuple[int, bytes]] = {}
    cdp_row(
        mib, address_type=integer(1), address=octets(bytes([10, 9, 8, 7])), version=octets(b"\xff" * 255 + ("\U0001f600" * 255).encode()),
        device_id=octets(b"\xfe" * 255), device_port=octets(b"\xfd" * 255), platform=octets(("\U0001f600" * 255).encode()),
        capabilities=octets(bytes([0, 0, 0, 0x28])), native_vlan=integer(42),
    )
    mib[f"{IFX}.1.3"] = octets("\U0001f600" * 255)
    scans = collect_neighbors(FakeWalker(mib), PLAN)
    records = _queue_records(scans)
    assert all(size <= 7_000 for size in _wire_size(records))


def test_a_neighbor_whose_identity_alone_is_oversize_is_dropped_and_the_scan_is_incomplete():
    big = {
        "protocol": "cdp", "local_port": {"name": "é" * 255, "ref": "3"},
        "remote": {"chassis_id": "\U0001f600" * 255, "chassis_id_subtype": "cdp_device_id", "port_id": "\U0001f600" * 255,
                   "port_id_subtype": "interface_name", "port_description": None, "system_name": None, "system_description": None,
                   "platform": None, "management_address": None},
        "capabilities": [], "native_vlan": None, "ttl_seconds": None, "raw": {},
    }
    from edge_collector.neighbors import NeighborScan, fit_observation

    assert fit_observation(big, scan_id="s") is None
    records = _queue_records([NeighborScan("cdp", (big,), 0, True)])
    assert [r.payload["record_type"] for r in records] == ["neighbor_scan"]
    marker = records[0].payload["raw_attributes"]
    assert marker["complete"] is False and marker["malformed_rows"] == 1 and marker["observed_count"] == 0
