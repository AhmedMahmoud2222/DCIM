"""The Compose smoke gate runs .github/scripts/clamd_probe.py inside the backend container.
These tests run the same script against a fake clamd socket so its failure modes are proven."""

import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[3] / ".github" / "scripts" / "clamd_probe.py"
BACKEND = Path(__file__).resolve().parents[2]


class FakeClamd:
    def __init__(self, *, detect_eicar: bool = True):
        self.detect_eicar = detect_eicar
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while True:
                    part = conn.recv(65536)
                    if not part:
                        break
                    data += part
                    if data.endswith(struct.pack("!I", 0)):
                        break
                found = self.detect_eicar and b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data
                conn.sendall(b"stream: Win.Test.EICAR_HDB-1 FOUND\0" if found else b"stream: OK\0")

    def close(self):
        self.server.close()


def run_probe(port: int, **extra: str) -> subprocess.CompletedProcess:
    env = {
        "DATABASE_URL": "postgresql+asyncpg://probe:probe@localhost:5432/probe",
        "REDIS_URL": "redis://localhost:6379/1",
        "JWT_SECRET_KEY": "test-only-secret-key-not-for-production-use-32ch",
        "CREDENTIAL_ENCRYPTION_KEY": "eHTZ8u6qF3v2N1oQwL9pR7sT4yU6iO0aB2cD5eF8gH0=",
        **os.environ,
        "CLAMD_HOST": "127.0.0.1",
        "CLAMD_PORT": str(port),
        "CLAMD_TIMEOUT_SECONDS": "3",
        "CATALOG_PDF_SCAN_MODE": "required",
        "EXPECT_CLAMD_HOST": "127.0.0.1",
        "EXPECT_CLAMD_PORT": str(port),
        "PYTHONPATH": str(BACKEND),
        **extra,
    }
    return subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True, text=True, timeout=60, cwd=BACKEND)


@pytest.fixture
def clamd():
    server = FakeClamd()
    yield server
    server.close()


def test_probe_accepts_clean_and_detects_eicar(clamd):
    result = run_probe(clamd.port, EXPECT_CLAMD_IPS="127.0.0.1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EICAR detected" in result.stdout


def test_probe_fails_when_clamd_unreachable():
    idle = socket.socket()
    idle.bind(("127.0.0.1", 0))
    port = idle.getsockname()[1]
    idle.close()
    assert run_probe(port).returncode == 12


def test_probe_fails_when_eicar_is_not_detected():
    server = FakeClamd(detect_eicar=False)
    try:
        assert run_probe(server.port).returncode == 14
    finally:
        server.close()


def test_probe_fails_when_endpoint_is_not_the_compose_container(clamd):
    assert run_probe(clamd.port, EXPECT_CLAMD_IPS="10.99.99.99").returncode == 11


def test_probe_fails_when_scan_mode_is_not_required(clamd):
    assert run_probe(clamd.port, CATALOG_PDF_SCAN_MODE="optional").returncode == 10
