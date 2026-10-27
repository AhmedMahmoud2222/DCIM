# ruff: noqa: E501
"""Connection-authenticated cancellation of the migration's own database session. There is NO PID-only signalling here.

Why not `pg_cancel_backend(pid)` / `pg_terminate_backend(pid)`: in PostgreSQL 16 `pg_signal_backend()` (signalfuncs.c) looks the
backend up by PID, checks permissions, and then calls `kill(-pid, sig)`. Its own comment concedes the window: the process "we
just validated above" may end and the PID be recycled "before reaching here", and "there's no way to acquire a lock on an
arbitrary process to prevent that". Any check done in SQL beforehand (database name, owner tag, `backend_start`, a single
wrapping statement) runs *before* that lookup and cannot close the window. So the benchmark does not use those functions at all.

What it uses instead is the protocol's own cancellation mechanism
(https://www.postgresql.org/docs/16/protocol-flow.html#PROTOCOL-FLOW-CANCELING-REQUESTS): every backend hands its client a
secret `BackendKeyData` (pid + cancel key) at connection start, and the postmaster honours a CancelRequest only when BOTH
match a backend it is currently running (`processCancelRequest`, postmaster.c: `bp->pid == backendPID && bp->cancel_key ==
cancelAuthCode`, then SIGINT to that child). A recycled PID has a different key, so a stale or guessed request is ignored.

To hold the key of the *Alembic* session (a subprocess whose connection we do not own) the benchmark routes that one connection
through `PgProxy`, a transparent TCP relay that reads the server's `BackendKeyData` message as it passes by and otherwise
forwards bytes untouched. Cancellation is then "send a CancelRequest with the key of the connection we proxied".

Residual risks, stated precisely:
* Only CANCEL is offered. PostgreSQL has no authenticated terminate, so there is no terminate scenario (see scenarios.s4).
* The cancel key is 32 bits in PostgreSQL 16. It authenticates against recycled PIDs and accidents; it is not a defence against
  an adversary who can see the key or guess among 2^32 values on the server's network path. The benchmark cluster is scratch.
* The relay must see the cleartext startup. If the server answers the SSLRequest with 'S' (TLS), the key cannot be read; the
  proxy records that and the scenario refuses to run (fail closed) rather than falling back to a PID.
* Cancel interrupts the connection's CURRENT statement, like `pg_cancel_backend`; if the build already finished, nothing happens.
* The relay adds a hop for that one connection. It is used only in the cancellation scenario, never in timed measurements.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass

CANCEL_REQUEST_CODE = 80877102
SSL_REQUEST_CODE = 80877103


@dataclass(frozen=True)
class BackendKey:
    pid: int
    secret: int


class NoBackendKey(RuntimeError):
    """The proxied connection's BackendKeyData was not (or could not be) observed; cancellation is not possible."""


def send_cancel_request(host: str, port: int, key: BackendKey, timeout: float = 10.0) -> None:
    """Send a protocol CancelRequest for `key` (the same 16 bytes libpq's PQcancel sends). Only an exact (pid, secret) match acts."""
    packet = struct.pack("!iiii", 16, CANCEL_REQUEST_CODE, key.pid, key.secret)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(packet)
        try:
            sock.recv(1)  # the server closes the connection without a reply
        except OSError:
            pass


class PgProxy:
    """A transparent single-purpose TCP relay in front of one PostgreSQL server that records each connection's BackendKeyData."""

    def __init__(self, upstream_host: str, upstream_port: int, listen_host: str = "127.0.0.1"):
        self.upstream = (upstream_host, upstream_port)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((listen_host, 0))
        self._listener.listen(8)
        self.host, self.port = self._listener.getsockname()
        self.keys: list[BackendKey] = []
        self.tls_seen = False
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        accept = threading.Thread(target=self._accept_loop, daemon=True)
        accept.start()
        self._threads.append(accept)

    # -- lifecycle -------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._closing.set()
        for sock in [self._listener, *self._sockets]:
            try:
                sock.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self) -> PgProxy:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- relay -----------------------------------------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._closing.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            try:
                server = socket.create_connection(self.upstream, timeout=10)
            except OSError:
                client.close()
                continue
            server.settimeout(None)
            client.settimeout(None)
            state = {"ssl_requested": False, "startup_error": None, "classified": False}
            with self._lock:
                self._sockets += [client, server]
            for target, args in ((self._client_to_server, (client, server, state)), (self._server_to_client, (server, client, state))):
                t = threading.Thread(target=target, args=args, daemon=True)
                t.start()
                self._threads.append(t)

    @staticmethod
    def _shut(*socks: socket.socket) -> None:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _client_to_server(self, client: socket.socket, server: socket.socket, state: dict) -> None:
        # Stream-based framing for the very first request. PostgreSQL's SSLRequest (and any startup message) may arrive split
        # across multiple TCP segments, so we MUST NOT classify or forward a partial 8-byte header. Buffer until at least the
        # 8-byte header is available, classify once, then forward the buffered bytes plus everything that follows untouched.
        buffer = bytearray()
        classified = False
        max_startup_bytes = 1 << 20  # 1 MiB: a startup message larger than this is not a valid PostgreSQL startup
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    if not classified:
                        state["startup_error"] = "incomplete or aborted startup: connection closed before 8 bytes"
                    break
                if not classified:
                    buffer.extend(data)
                    if len(buffer) < 8:
                        # Hold the partial header; do not forward yet, otherwise the server could see a truncated request.
                        continue
                    try:
                        length, code = struct.unpack("!ii", bytes(buffer[:8]))
                    except struct.error as exc:
                        state["startup_error"] = f"malformed startup header: {exc}"
                        raise
                    if length < 8 or length > max_startup_bytes:
                        state["startup_error"] = f"malformed startup: invalid length {length}"
                        raise ValueError(state["startup_error"])
                    state["ssl_requested"] = (length == 8 and code == SSL_REQUEST_CODE)
                    state["classified"] = True
                    classified = True
                    # Forward the entire buffered prefix (header + any trailing bytes) in original order, exactly once.
                    if buffer:
                        server.sendall(bytes(buffer))
                        buffer.clear()
                else:
                    server.sendall(data)
        except (OSError, ValueError, struct.error) as exc:
            if state.get("startup_error") is None:
                state["startup_error"] = f"aborted startup: {exc}"
        finally:
            self._shut(client, server)

    def _server_to_client(self, server: socket.socket, client: socket.socket, state: dict) -> None:
        buffer = b""
        looking = True  # parse messages only until BackendKeyData has gone by
        ssl_reply_pending = True
        try:
            while True:
                data = server.recv(65536)
                if not data:
                    break
                client.sendall(data)
                if not looking:
                    continue
                buffer += data
                if ssl_reply_pending and state["ssl_requested"]:
                    ssl_reply_pending = False
                    reply, buffer = buffer[:1], buffer[1:]
                    if reply == b"S":  # TLS: the stream is opaque from here, so the key can never be read
                        self.tls_seen = True
                        looking = False
                        continue
                elif ssl_reply_pending:
                    ssl_reply_pending = False
                while looking and len(buffer) >= 5:
                    kind, length = buffer[:1], struct.unpack("!i", buffer[1:5])[0]
                    if len(buffer) < 1 + length:
                        break
                    body = buffer[5 : 1 + length]
                    buffer = buffer[1 + length :]
                    if kind == b"K" and len(body) == 8:
                        pid, secret = struct.unpack("!ii", body)
                        with self._lock:
                            self.keys.append(BackendKey(pid, secret))
                        looking = False
        except OSError:
            pass
        finally:
            self._shut(client, server)

    # -- use -------------------------------------------------------------------------------------------------------
    def only_key(self, timeout: float = 30.0) -> BackendKey:
        """The BackendKeyData of the single proxied connection; raises NoBackendKey rather than guessing among several."""
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.tls_seen:
                    raise NoBackendKey("the server negotiated TLS, so the relay cannot read BackendKeyData; refusing to cancel")
                if len(self.keys) == 1:
                    return self.keys[0]
                if len(self.keys) > 1:
                    raise NoBackendKey(f"{len(self.keys)} connections were proxied; refusing to pick one")
            time.sleep(0.05)
        raise NoBackendKey("no BackendKeyData observed on the proxied connection")

    def cancel(self, key: BackendKey | None = None) -> BackendKey:
        key = key or self.only_key()
        send_cancel_request(*self.upstream, key)
        return key
