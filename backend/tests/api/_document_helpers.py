"""Shared fixtures for the datasheet-document tests: synthetic PDFs (generated, never
vendor files), a controllable fake malware scanner, and catalog setup helpers."""

import io
import uuid

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.application.catalog_documents.malware_scan import ScannerUnavailable


def make_pdf(text: str = "Datasheet", pages: int = 1) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(pages):
        pdf.drawString(72, 720, f"{text} page {index + 1} {uuid.uuid4().hex if index == 0 and text == 'unique' else ''}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def make_blank_pdf(pages: int = 1) -> PdfWriter:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    return writer


def to_bytes(writer: PdfWriter) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_with_javascript() -> bytes:
    writer = make_blank_pdf()
    action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('x');"),
        }
    )
    writer._root_object[NameObject("/OpenAction")] = writer._add_object(action)
    return to_bytes(writer)


def pdf_with_attachment() -> bytes:
    writer = make_blank_pdf()
    writer.add_attachment("payload.exe", b"MZ....")
    return to_bytes(writer)


def pdf_with_open_action() -> bytes:
    writer = make_blank_pdf()
    action = DictionaryObject({NameObject("/S"): NameObject("/Launch"), NameObject("/F"): TextStringObject("cmd.exe")})
    writer._root_object[NameObject("/OpenAction")] = writer._add_object(action)
    return to_bytes(writer)


def pdf_encrypted() -> bytes:
    writer = make_blank_pdf()
    writer.encrypt("secret")
    return to_bytes(writer)


class FakeScanner:
    engine_name = "fake-clamd"

    def __init__(self) -> None:
        self.mode = "clean"  # clean | infected | down
        self.scanned: list[int] = []

    def scan(self, content: bytes) -> str | None:
        self.scanned.append(len(content))
        if self.mode == "down":
            raise ScannerUnavailable("down")
        return "Eicar-Test-Signature" if self.mode == "infected" else None


async def make_manufacturer(client, headers, name: str | None = None) -> str:
    resp = await client.post(
        "/api/v1/catalog/manufacturers", json={"name": name or f"Docs Co {uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def make_model(client, headers, manufacturer_id: str, *, category: str = "rack") -> str:
    resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": category, "model_name": f"Model-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def make_draft(client, headers, model_id: str) -> dict:
    resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_published_rack_revision(client, headers, model_id: str) -> dict:
    draft = await make_draft(client, headers, model_id)
    body = {
        "dimension_unit": "in", "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4,
        "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220.0,
    }
    resp = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}", json=body, headers={**headers, "If-Match": str(draft["version"])}
    )
    assert resp.status_code == 200, resp.text
    published = await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    return published.json()


def pdf_files(content: bytes, name: str = "datasheet.pdf") -> dict:
    return {"file": (name, content, "application/pdf")}
