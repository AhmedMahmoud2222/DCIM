"""Regression tests for stream-based PostgreSQL SSLRequest framing in cancel.PgProxy.

These tests exercise the relay in isolation (fake upstream servers) so they do not
need a live PostgreSQL instance, except where noted.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from scripts.fk_index_benchmark.cancel import BackendKey, NoBackendKey, PgProxy


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("short read")
        buf += chunk
    return buf


def _fake_server(handler):
    fake = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fake.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    fake.bind(("127.0.0.1", 0))
    fake.listen(1)
    thread = threading.Thread(target=handler, args=(fake,), daemon=True)
    thread.start()
    return fake, thread


def test_complete_ssl_request_in_one_read_is_classified():
    def handler(fake):
        conn, _ = fake.accept()
        req = _read_exact(conn, 8)
        assert struct.unpack("!ii", req) == (8, 80877103)
        conn.sendall(b"S")
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                client.sendall(struct.pack("!ii", 8, 80877103))
                assert client.recv(1) == b"S"
            with pytest.raises(NoBackendKey, match="TLS"):
                proxy.only_key(timeout=2)
    finally:
        thread.join(timeout=2)
        fake.close()


def test_fragmented_ssl_request_byte_by_byte_is_classified():
    def handler(fake):
        conn, _ = fake.accept()
        req = _read_exact(conn, 8)
        assert struct.unpack("!ii", req) == (8, 80877103)
        conn.sendall(b"S")
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                for byte in struct.pack("!ii", 8, 80877103):
                    client.sendall(bytes([byte]))
                    time.sleep(0.01)
                assert client.recv(1) == b"S"
            with pytest.raises(NoBackendKey, match="TLS"):
                proxy.only_key(timeout=2)
    finally:
        thread.join(timeout=2)
        fake.close()


def test_fragmented_ssl_request_in_two_chunks_is_classified():
    def handler(fake):
        conn, _ = fake.accept()
        req = _read_exact(conn, 8)
        assert struct.unpack("!ii", req) == (8, 80877103)
        conn.sendall(b"S")
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                payload = struct.pack("!ii", 8, 80877103)
                client.sendall(payload[:4])
                time.sleep(0.05)
                client.sendall(payload[4:])
                assert client.recv(1) == b"S"
            with pytest.raises(NoBackendKey, match="TLS"):
                proxy.only_key(timeout=2)
    finally:
        thread.join(timeout=2)
        fake.close()


def test_tls_acceptance_results_in_explicit_cancellation_refusal():
    def handler(fake):
        conn, _ = fake.accept()
        _read_exact(conn, 8)
        conn.sendall(b"S")
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                client.sendall(struct.pack("!ii", 8, 80877103))
                assert client.recv(1) == b"S"
            with pytest.raises(NoBackendKey, match="TLS"):
                proxy.only_key(timeout=2)
            assert proxy.tls_seen is True
    finally:
        thread.join(timeout=2)
        fake.close()


def test_ssl_refusal_then_plaintext_startup_reads_backend_key():
    def handler(fake):
        conn, _ = fake.accept()
        req = _read_exact(conn, 8)
        assert struct.unpack("!ii", req) == (8, 80877103)
        conn.sendall(b"N")
        hdr = _read_exact(conn, 4)
        (length,) = struct.unpack("!i", hdr)
        _read_exact(conn, length - 4)
        conn.sendall(b"K" + struct.pack("!i", 12) + struct.pack("!ii", 4242, 9911))
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                client.sendall(struct.pack("!ii", 8, 80877103))
                assert client.recv(1) == b"N"
                body = b"user\x00postgres\x00\x00"
                startup = struct.pack("!i", 8 + len(body)) + struct.pack("!i", 196608) + body
                client.sendall(startup)
                key = proxy.only_key(timeout=5)
                assert key == BackendKey(4242, 9911)
    finally:
        thread.join(timeout=2)
        fake.close()


def test_ordinary_non_tls_startup_reads_backend_key():
    def handler(fake):
        conn, _ = fake.accept()
        hdr = _read_exact(conn, 4)
        (length,) = struct.unpack("!i", hdr)
        _read_exact(conn, length - 4)
        conn.sendall(b"K" + struct.pack("!i", 12) + struct.pack("!ii", 7, 11))
        time.sleep(0.3)
        conn.close()

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                body = b"user\x00postgres\x00\x00"
                startup = struct.pack("!i", 8 + len(body)) + struct.pack("!i", 196608) + body
                client.sendall(startup)
                key = proxy.only_key(timeout=5)
                assert key == BackendKey(7, 11)
    finally:
        thread.join(timeout=2)
        fake.close()


def test_partial_startup_connection_does_not_crash_and_refuses():
    def handler(fake):
        conn, _ = fake.accept()
        time.sleep(1.0)
        try:
            conn.close()
        except OSError:
            pass

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                client.sendall(b"\x00\x00\x00")  # only 3 bytes: incomplete header
            with pytest.raises(NoBackendKey):
                proxy.only_key(timeout=2)
    finally:
        thread.join(timeout=3)
        fake.close()


def test_aborted_startup_after_full_header_does_not_crash():
    def handler(fake):
        conn, _ = fake.accept()
        _read_exact(conn, 8)
        time.sleep(0.5)
        try:
            conn.close()
        except OSError:
            pass

    fake, thread = _fake_server(handler)
    try:
        with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
            with socket.create_connection((proxy.host, proxy.port)) as client:
                client.sendall(struct.pack("!ii", 8, 80877103))
            with pytest.raises(NoBackendKey):
                proxy.only_key(timeout=2)
    finally:
        thread.join(timeout=3)
        fake.close()
