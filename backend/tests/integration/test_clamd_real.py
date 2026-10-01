"""ClamdScanner against a real `clamd` process, with a tiny local signature database (the
EICAR test file's hash) so no signature download is needed. Skipped where clamd is not
installed (`apt-get install clamav-daemon`)."""

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from app.application.catalog_documents.malware_scan import ClamdScanner, ScannerUnavailable

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR_MD5 = "44d88612fea8a8f36de82e1278abb02f"

pytestmark = pytest.mark.skipif(shutil.which("clamd") is None, reason="clamd is not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def clamd_port():
    import tempfile

    # Not pytest's tmp_path: its parents are 0700, which the unprivileged clamav user (clamd
    # refuses to run as root) could not traverse.
    tmp_dir = tempfile.mkdtemp(prefix="dcim-clamd-")
    os.chmod(tmp_dir, 0o755)
    tmp_path = Path(tmp_dir)
    database = tmp_path / "db"
    database.mkdir()
    (database / "eicar.hdb").write_text(f"{EICAR_MD5}:{len(EICAR)}:Eicar-Test-Signature\n")
    # Body signature so the EICAR string is also found when embedded inside a valid PDF.
    (database / "eicar-body.ndb").write_text(f"Eicar-Body:0:*:{EICAR.hex()}\n")
    port = _free_port()
    config = tmp_path / "clamd.conf"
    lines = [
        f"DatabaseDirectory {database}", f"TCPSocket {port}", "TCPAddr 127.0.0.1", "Foreground yes",
        f"LogFile {tmp_path / 'clamd.log'}", f"PidFile {tmp_path / 'clamd.pid'}", "StreamMaxLength 25M",
    ]
    if os.geteuid() == 0:
        import pwd

        try:
            user = pwd.getpwnam("clamav")
        except KeyError:
            pytest.skip("no clamav user to drop privileges to")
        for path in (tmp_path, database):
            os.chown(path, user.pw_uid, user.pw_gid)
        for signature_file in database.iterdir():
            os.chown(signature_file, user.pw_uid, user.pw_gid)
        lines.append("User clamav")
    config.write_text("\n".join(lines) + "\n")
    process = subprocess.Popen(["clamd", "--config-file", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603, S607
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.skip("clamd exited during startup")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.skip("clamd did not start in time")
        yield port
    finally:
        process.terminate()
        process.wait(10)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_real_clamd_detects_eicar_and_passes_clean_pdf(clamd_port):
    from tests.api._document_helpers import make_pdf

    scanner = ClamdScanner("127.0.0.1", clamd_port, 30)
    assert scanner.scan(make_pdf("clean datasheet")) is None
    assert "Eicar" in (scanner.scan(EICAR) or "")


def _raw_instream(port: int, content: bytes) -> tuple[bytes, bytes]:
    """The bytes a real clamd sends back for `zINSTREAM`, and whatever follows them (b"" means it closed)."""
    import struct

    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.sendall(b"zINSTREAM\0" + struct.pack("!I", len(content)) + content + struct.pack("!I", 0))
        reply = b""
        while not reply.endswith(b"\0"):
            part = sock.recv(4096)
            assert part, f"clamd closed without terminating its reply: {reply!r}"
            reply += part
        sock.settimeout(5)
        return reply, sock.recv(4096)


def test_real_clamd_reply_framing_is_what_the_strict_parser_expects(clamd_port):
    """Positive control for the parser: a real daemon's raw reply is one NUL-terminated line followed by
    EOF, for a clean file and for an infected one."""
    reply, after = _raw_instream(clamd_port, b"%PDF-1.4 harmless")
    assert reply == b"stream: OK\0" and after == b""
    reply, after = _raw_instream(clamd_port, EICAR)
    assert re.fullmatch(rb"stream: Eicar[\x21-\x7e]* FOUND\0", reply), reply
    assert after == b""
    signature = reply[len(b"stream: ") : -len(b" FOUND\0")].decode()
    assert ClamdScanner("127.0.0.1", clamd_port, 30).scan(EICAR) == signature


def test_real_clamd_size_limit_error_is_no_verdict(clamd_port):
    """StreamMaxLength is 25M in this fixture: one byte more makes clamd answer with an ERROR line and
    drop the stream. The scanner must report no verdict, never clean."""
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", clamd_port, 60).scan(b"0" * (25 * 1024 * 1024 + 1024))


def test_real_clamd_scans_a_25mb_stream(clamd_port):
    scanner = ClamdScanner("127.0.0.1", clamd_port, 60)
    assert scanner.scan(b"%PDF-1.4\n" + b"0" * (25 * 1024 * 1024 - 100)) is None


def test_real_clamd_going_away_fails_as_unavailable(clamd_port):
    assert ClamdScanner("127.0.0.1", clamd_port, 5).scan(b"abc") is None
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", _free_port(), 1).scan(b"abc")


async def test_api_upload_end_to_end_with_real_clamd(clamd_port, client, auth_headers, db_session):
    """Upload through the HTTP API with the production scanner class against a real daemon:
    clean PDF stored, EICAR-carrying PDF rejected/audited/not stored, daemon down fails closed."""
    from sqlalchemy import select, text

    from app.api.v1.catalog_documents import get_malware_scanner
    from app.domain.audit.models import AuditLog
    from app.main import app
    from tests.api._document_helpers import make_pdf, pdf_files

    scanners = {"port": clamd_port}
    app.dependency_overrides[get_malware_scanner] = lambda: ClamdScanner("127.0.0.1", scanners["port"], 30)
    try:
        headers = await auth_headers("Administrator")
        clean = await client.post("/api/v1/catalog/documents", files=pdf_files(make_pdf("clean")), headers=headers)
        assert clean.status_code == 201, clean.text
        assert clean.json()["scan_status"] == "clean" and clean.json()["scan_engine"] == "clamd"

        from pypdf.generic import DecodedStreamObject, NameObject

        from tests.api._document_helpers import make_blank_pdf, to_bytes

        writer = make_blank_pdf()
        payload = DecodedStreamObject()
        payload.set_data(EICAR)  # stored uncompressed, so clamd sees the raw bytes in a structurally valid PDF
        writer._root_object[NameObject("/Payload")] = writer._add_object(payload)
        infected_pdf = to_bytes(writer)
        assert EICAR in infected_pdf
        infected = await client.post("/api/v1/catalog/documents", files=pdf_files(infected_pdf), headers=headers)
        assert infected.status_code == 422 and "malware" in infected.json()["detail"], infected.text
        assert (await db_session.execute(text("SELECT count(*) FROM catalog_document"))).scalar_one() == 1
        actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
        assert "catalog.document.upload_rejected_malware" in actions

        scanners["port"] = _free_port()  # daemon unreachable
        down = await client.post("/api/v1/catalog/documents", files=pdf_files(make_pdf("while down")), headers=headers)
        assert down.status_code == 503, down.text
        assert (await db_session.execute(text("SELECT count(*) FROM catalog_document"))).scalar_one() == 1
    finally:
        app.dependency_overrides.pop(get_malware_scanner, None)
