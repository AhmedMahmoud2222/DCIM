"""Deterministic PgProxy registration and ambiguity regressions for issue #260. No PostgreSQL is needed."""

from __future__ import annotations

import contextlib
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
JOIN = 5.0


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
        self._closed = False
        self._serve_threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            n = self._count
            self._count += 1
            self._conns.append(conn)
            thread = threading.Thread(target=self._serve, args=(conn, n), daemon=True)
            self._serve_threads.append(thread)
            thread.start()

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
        """Unblock and join every fake thread, then prove the listener port refuses new connections."""
        if self._closed:
            return
        self._closed = True
        _shutdown_close(self._listener)  # shutdown wakes the blocked accept(); close alone does not
        self._accept_thread.join(JOIN)  # after this no new connection can be tracked
        for conn in self._conns:
            _shutdown_close(conn)  # wakes serve threads blocked in recv()
        with self._guard:
            for event in self._consumed.values():
                event.set()  # wakes serve threads waiting for the relay's first read
        for thread in self._serve_threads:
            thread.join(JOIN)
        alive = [t for t in [self._accept_thread, *self._serve_threads] if t.is_alive()]
        assert not alive, f"{len(alive)} fake upstream thread(s) still alive after close()"
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            return
        raise AssertionError("fake upstream listener still accepts connections after close()")


def _shutdown_close(sock: socket.socket) -> None:
    for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            action()
        except OSError:
            pass


@contextlib.contextmanager
def running_proxy(upstream_port: int):
    """A PgProxy whose teardown wakes every relay thread (shutdown, then close) and fails if any is still alive afterwards.

    PgProxy.close() only close()s sockets, which does not interrupt a recv()/accept() blocked in another thread, so without the
    shutdown first its join() would run into its timeout. Callers must close their clients and release any gate before leaving.
    """
    proxy = PgProxy("127.0.0.1", upstream_port)
    try:
        yield proxy
    finally:
        for sock in [proxy._listener, *list(proxy._sockets)]:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        proxy.close()
        alive = [t for t in list(proxy._threads) if t.is_alive()]
        assert not alive, f"{len(alive)} proxy relay thread(s) still alive after close()"


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
def _clean_tables():
    """Socket-only tests do not use a database; shadow the repository-wide autouse cleanup fixture."""
    yield


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


def test_key_is_registered_before_ready_for_query_can_reach_client(split_upstream, coalesce_upstream_reads):
    """A gated registration must block the second connection's response, and ambiguity must fail closed meanwhile."""
    upstream = split_upstream
    hook, wrappers = coalesce_upstream_reads
    hook.upstream = upstream
    clients: list[socket.socket] = []
    with running_proxy(upstream.port) as proxy:
        gate = GatedLock(proxy._lock)
        proxy._lock = gate
        try:
            clients.append(_connect_and_read_ready(proxy))
            _wait_for_keys(proxy, 1)
            assert proxy.only_key(timeout=WAIT) == BackendKey(1000, 2000)

            gate.armed = True
            connected = threading.Event()
            result: dict = {}

            def connect_second():
                try:
                    result["client"] = _connect_and_read_ready(proxy)
                    connected.set()
                except BaseException as exc:  # surface helper-thread failures in the test thread
                    result["error"] = exc
                    connected.set()

            thread = threading.Thread(target=connect_second, daemon=True)
            thread.start()
            assert gate.reached.wait(WAIT), "the relay never reached the registration point for the second connection"
            assert not connected.wait(0.1), "the second connection received ReadyForQuery before its key was registered"
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=WAIT)
            gate.release.set()
            thread.join(JOIN)
            assert not thread.is_alive(), "second client did not finish after key registration was released"
            assert "error" not in result, result.get("error")
            clients.append(result["client"])
            if upstream.split:
                assert all(w.reads >= 2 for w in wrappers), "split mode did not actually split the relay's reads"
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
    with running_proxy(upstream.port) as proxy:
        try:
            clients.append(_connect_and_read_ready(proxy))
            clients.append(_connect_and_read_ready(proxy))
            _wait_for_keys(proxy, 2)
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=WAIT)
        finally:
            for client in clients:
                client.close()


def test_pending_second_handshake_prevents_returning_first_key(upstream):
    """A second accepted socket is ambiguous even before it completes startup or exposes a key."""
    clients: list[socket.socket] = []
    with running_proxy(upstream.port) as proxy:
        try:
            clients.append(_connect_and_read_ready(proxy))
            pending = socket.create_connection((proxy.host, proxy.port), timeout=WAIT)
            clients.append(pending)
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                with proxy._lock:
                    if len(proxy._connections) == 2:
                        break
                time.sleep(0.01)
            else:
                pytest.fail("proxy did not register the second accepted connection")
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=WAIT)
        finally:
            for client in clients:
                client.close()


def test_upstream_socket_failure_fails_closed():
    unavailable = socket.socket()
    unavailable.bind(("127.0.0.1", 0))
    port = unavailable.getsockname()[1]
    unavailable.close()
    with running_proxy(port) as proxy:
        with socket.create_connection((proxy.host, proxy.port), timeout=WAIT):
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                with proxy._lock:
                    if proxy._connections and proxy._connections[0]["status"] == "failed":
                        break
                time.sleep(0.01)
            else:
                pytest.fail("upstream connection failure was not recorded")
            with pytest.raises(NoBackendKey, match="closed|handshake"):
                proxy.only_key(timeout=WAIT)


def test_tls_handshake_is_forwarded_but_cannot_expose_a_key():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    finished = threading.Event()

    def serve_tls_request():
        try:
            upstream, _ = listener.accept()
            with upstream:
                request = b""
                while len(request) < 8:
                    request += upstream.recv(8 - len(request))
                assert request == struct.pack("!ii", 8, 80877103)
                upstream.sendall(b"S")
                upstream.recv(1)
        finally:
            finished.set()

    server = threading.Thread(target=serve_tls_request, daemon=True)
    server.start()
    try:
        with running_proxy(port) as proxy:
            with socket.create_connection((proxy.host, proxy.port), timeout=WAIT) as client:
                client.sendall(struct.pack("!ii", 8, 80877103))
                assert client.recv(1) == b"S"
                client.sendall(b"x")
            with pytest.raises(NoBackendKey, match="TLS"):
                proxy.only_key(timeout=WAIT)
            assert proxy.tls_seen
    finally:
        listener.close()
        server.join(JOIN)
        assert finished.is_set()


def test_incomplete_startup_never_becomes_a_cancel_target(upstream):
    with running_proxy(upstream.port) as proxy:
        with socket.create_connection((proxy.host, proxy.port), timeout=WAIT) as client:
            client.sendall(STARTUP[:3])
        with pytest.raises(NoBackendKey):
            proxy.only_key(timeout=WAIT)
