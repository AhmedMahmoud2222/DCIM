"""Deterministic witness for the PgProxy BackendKeyData registration window (issue #260). No PostgreSQL is needed.

`PgProxy._server_to_client` forwards the server's bytes to the client (`client.sendall`) BEFORE it parses them and appends the
BackendKey under `self._lock`. A client that has seen ReadyForQuery therefore may call `only_key()` while that connection's key is
not registered yet. This module pins that window with a gated lock instead of timing, and keeps the safety property separately.

* `test_witness_*` is a DIAGNOSTIC reproducer: it asserts the CURRENT, unsafe observation (one key returned while two connections
  are established). If the relay is later changed to register before forwarding, it fails with an explicit "obsolete" message and
  should then be removed or inverted. It proves the window exists; it says nothing about how often CI hits it.
  It is independent of TCP receive boundaries: the relay's upstream socket is wrapped so that its first read always ends after
  ReadyForQuery, whether the fake server writes the response in one piece or splits it between BackendKeyData and ReadyForQuery.
* `test_safety_*` is a passing SAFETY regression: once both keys are registered, `only_key` must refuse to pick one.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time

import pytest

from scripts.fk_index_benchmark import cancel as cancel_module
from scripts.fk_index_benchmark.cancel import BackendKey, NoBackendKey, PgProxy

STARTUP = struct.pack("!ii", 8, 196608)
READY_FOR_QUERY = b"Z" + struct.pack("!i", 5) + b"I"
WAIT = 5.0


class FakeUpstream:
    """Answers each startup packet with AuthenticationOk, BackendKeyData(pid=1000+n, secret=2000+n) and ReadyForQuery."""

    def __init__(self, split: bool = False) -> None:
        self.split = split  # True: write BackendKeyData and ReadyForQuery as two separate sends
        self._consumed: dict[tuple, threading.Event] = {}  # per proxy->upstream connection: relay has read the first part
        self._guard = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._conns: list[socket.socket] = []
        self._count = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            n = self._count
            self._count += 1
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn, n), daemon=True).start()

    def consumed_event(self, addr: tuple) -> threading.Event:
        with self._guard:
            return self._consumed.setdefault(addr, threading.Event())

    def _serve(self, conn: socket.socket, n: int) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            consumed = self.consumed_event(conn.getpeername())
            received = b""
            while len(received) < 8:
                chunk = conn.recv(8 - len(received))
                if not chunk:
                    return
                received += chunk
            head = b"R" + struct.pack("!ii", 8, 0) + b"K" + struct.pack("!iii", 12, 1000 + n, 2000 + n)
            if self.split:
                conn.sendall(head)
                # ReadyForQuery goes out only after the relay has read the first part, so the boundary is real, not left to TCP.
                if not consumed.wait(WAIT):
                    return
                conn.sendall(READY_FOR_QUERY)
            else:
                conn.sendall(head + READY_FOR_QUERY)
            while conn.recv(4096):
                pass
        except OSError:
            pass

    def close(self) -> None:
        for sock in [self._listener, *self._conns]:
            try:
                sock.close()
            except OSError:
                pass


class GatedLock:
    """Wraps the proxy's lock. When armed, parks ONLY the relay's `_server_to_client` thread on entry, i.e. just before `keys.append`."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.armed = False
        self.reached = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        if self.armed and sys._getframe(1).f_code.co_name == "_server_to_client":
            self.reached.set()
            self.release.wait(WAIT * 3)
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class CoalescingSocket:
    """The relay's upstream socket. Its first read is extended until ReadyForQuery has arrived, so one `recv` ends after it.

    This removes TCP segmentation from the experiment: whatever the fake server's write pattern, the relay sees the response as
    one chunk, so the lock gate is reached only after the chunk, including ReadyForQuery, was forwarded to the client.
    """

    def __init__(self, sock: socket.socket, on_partial) -> None:
        self._sock = sock
        self._on_partial = on_partial
        self._complete = False
        self.reads = 0

    def recv(self, bufsize: int) -> bytes:
        data = self._sock.recv(bufsize)
        self.reads += 1
        if self._complete or not data:
            return data
        while READY_FOR_QUERY not in data:
            self._on_partial(self._sock.getsockname())
            more = self._sock.recv(bufsize)
            self.reads += 1
            if not more:
                break
            data += more
        self._complete = True
        return data

    def __getattr__(self, name):
        return getattr(self._sock, name)


@pytest.fixture
def coalesce_upstream_reads(monkeypatch):
    """Make PgProxy's connections to the fake upstream CoalescingSockets. Returns (hook, wrappers); set hook.upstream first."""
    wrappers: list[CoalescingSocket] = []
    real = socket.create_connection

    def create_connection(address, *args, **kwargs):
        sock = real(address, *args, **kwargs)
        upstream = create_connection.upstream
        if upstream is None or tuple(address) != ("127.0.0.1", upstream.port):
            return sock
        wrapper = CoalescingSocket(sock, lambda addr: upstream.consumed_event(addr).set())
        wrappers.append(wrapper)
        return wrapper

    create_connection.upstream = None
    monkeypatch.setattr(cancel_module.socket, "create_connection", create_connection)
    return create_connection, wrappers


def _connect_and_read_ready(proxy: PgProxy) -> socket.socket:
    """Open a raw client through the proxy and read until ReadyForQuery, which is where psycopg.connect() returns."""
    client = socket.create_connection((proxy.host, proxy.port), timeout=WAIT)
    client.sendall(STARTUP)
    received = b""
    while READY_FOR_QUERY not in received:
        chunk = client.recv(4096)
        if not chunk:
            raise AssertionError("proxy closed the connection before ReadyForQuery")
        received += chunk
    return client


def _wait_for_keys(proxy: PgProxy, count: int) -> None:
    deadline = time.time() + WAIT
    while time.time() < deadline:
        with proxy._lock:
            if len(proxy.keys) >= count:
                return
        time.sleep(0.01)
    raise AssertionError(f"expected {count} registered keys within {WAIT}s, saw {len(proxy.keys)}")


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture(params=["whole", "split"])
def split_upstream(request):
    fake = FakeUpstream(split=request.param == "split")
    yield fake
    fake.close()


def test_witness_only_key_returns_one_key_while_second_connection_is_established(split_upstream, coalesce_upstream_reads):
    """DIAGNOSTIC: pins the current window; passing means the window exists, not that it caused the CI failure.

    Runs with the response written whole and split between BackendKeyData and ReadyForQuery; both give the same observation.
    """
    upstream = split_upstream
    hook, wrappers = coalesce_upstream_reads
    hook.upstream = upstream
    clients: list[socket.socket] = []
    with PgProxy("127.0.0.1", upstream.port) as proxy:
        gate = GatedLock(proxy._lock)
        proxy._lock = gate
        try:
            clients.append(_connect_and_read_ready(proxy))
            _wait_for_keys(proxy, 1)

            gate.armed = True
            try:
                # B's client receives ReadyForQuery while the relay thread is parked before registering B's key.
                clients.append(_connect_and_read_ready(proxy))
            except (TimeoutError, OSError) as exc:
                pytest.fail(f"witness obsolete: the relay no longer forwards ReadyForQuery before registering the key ({exc!r})")
            assert gate.reached.wait(WAIT), "the relay never reached the registration point for the second connection"
            if upstream.split:
                assert all(w.reads >= 2 for w in wrappers), "split mode did not actually split the relay's reads"
            assert len(proxy.keys) == 1  # two established connections, one registered key

            # The observation under test: the guard returns a single key although two connections are established.
            assert proxy.only_key(timeout=WAIT) == BackendKey(1000, 2000)

            gate.release.set()
            _wait_for_keys(proxy, 2)
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=WAIT)
        finally:
            gate.release.set()
            for client in clients:
                client.close()


def test_safety_only_key_refuses_once_both_connections_are_registered(upstream):
    """SAFETY regression: with two registered keys, only_key must refuse to pick one."""
    clients: list[socket.socket] = []
    with PgProxy("127.0.0.1", upstream.port) as proxy:
        try:
            clients.append(_connect_and_read_ready(proxy))
            clients.append(_connect_and_read_ready(proxy))
            _wait_for_keys(proxy, 2)
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=WAIT)
        finally:
            for client in clients:
                client.close()
