"""Upload through the real HTTP boundary against a scripted clamd peer: replies that are not a single
well-formed verdict must reject the upload under `required` (503) and store nothing. A genuine clean
reply is the positive control and a genuine infected reply still maps to 422."""

import hashlib

import pytest
from sqlalchemy import text

from app.infrastructure.storage import get_document_storage_backend
from tests.api._document_helpers import make_pdf, pdf_files
from tests.unit.test_malware_scan_protocol import ScriptedClamd

REPLIES = {
    "clean-control": (b"stream: OK\0", 201),
    "infected-control": (b"stream: Test-Signature FOUND\0", 422),
    "suffix-ok": (b"anything at all OK\0", 503),
    "unterminated-ok": (b"stream: OK", 503),
    "two-verdicts": (b"stream: OK\0stream: Test-Signature FOUND\0", 503),
    "empty": (b"", 503),
}


@pytest.mark.parametrize("name", sorted(REPLIES))
async def test_required_mode_over_http_only_accepts_a_well_formed_verdict(client, auth_headers, settings_override, db_session, name):
    reply, status = REPLIES[name]
    admin = await auth_headers("Administrator")
    peer = ScriptedClamd(reply)
    try:
        settings_override(clamd_host="127.0.0.1", clamd_port=peer.port, clamd_timeout_seconds=2.0, catalog_pdf_scan_mode="required")
        content = make_pdf(f"scanner protocol {name} unique")
        resp = await client.post("/api/v1/catalog/documents", files=pdf_files(content, "ds.pdf"), headers=admin)
    finally:
        peer.close()
    assert resp.status_code == status, resp.text
    sha = hashlib.sha256(content).hexdigest()
    rows = (await db_session.execute(text("SELECT count(*) FROM catalog_document WHERE sha256 = :s"), {"s": sha})).scalar_one()
    stored = get_document_storage_backend().exists(f"{sha}.pdf")
    assert (rows, stored) == ((1, True) if status == 201 else (0, False))
    if status == 503:
        assert "OK" not in resp.json().get("detail", "")  # the reply is never echoed back
