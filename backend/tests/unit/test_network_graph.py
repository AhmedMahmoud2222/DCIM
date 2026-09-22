import uuid

from app.application.network_graph import GraphInterface, trace_to_core


def interface(device, name):
    return GraphInterface(uuid.uuid5(uuid.NAMESPACE_DNS, f"{device}-{name}"), device, name)


def test_deterministic_trace_reaches_core():
    endpoint, access, core = (uuid.uuid4() for _ in range(3))
    eth = interface(endpoint, "eth0")
    server = interface(access, "Gi1/0/24")
    uplink = interface(access, "Gi1/0/1")
    core_port = interface(core, "Gi1/1/1")
    state, path = trace_to_core(
        endpoint, [core_port, uplink, server, eth], [(uplink.id, core_port.id), (eth.id, server.id)], {core}
    )
    assert state == "complete"
    assert path == [eth.id, server.id, uplink.id, core_port.id]


def test_loop_is_bounded_and_incomplete():
    first, second = uuid.uuid4(), uuid.uuid4()
    a = interface(first, "a")
    b = interface(second, "b")
    assert trace_to_core(first, [a, b], [(a.id, b.id), (b.id, a.id)], set()) == ("incomplete", [])


def test_missing_interfaces_is_unknown():
    source = uuid.uuid4()
    assert trace_to_core(source, [], [], set()) == ("unknown", [])


def test_malformed_self_link_is_ignored():
    source = uuid.uuid4()
    a = interface(source, "a")
    assert trace_to_core(source, [a], [(a.id, a.id)], set()) == ("incomplete", [])
