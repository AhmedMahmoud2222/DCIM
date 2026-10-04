"""Synthetic datasheets for the extraction tests: generated here, never vendor files.

`native_pdf` draws real text (one list of lines per page); `table_pdf` draws cells at fixed
x positions so the layout text carries the column gaps; `scanned_pdf` renders the text to a bitmap
and embeds only the image, so the page has no text layer at all; the `*_bomb` builders produce the
hostile shapes the sandbox limits exist for."""

import io
import zlib

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import (
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def native_pdf(pages: list[list[str]]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for lines in pages:
        y = 790
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def table_pdf(pages: list[list[list[str]]], *, column_width: int = 130) -> bytes:
    """`pages[p][row][col]`: each cell drawn at its own x, so cells are separated by wide gaps."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(1400, 842))
    pdf.setFont("Helvetica", 9)
    for rows in pages:
        y = 800
        for row in rows:
            for column, cell in enumerate(row):
                pdf.drawString(30 + column * column_width, y, cell)
            y -= 16
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def scanned_pdf(pages: list[list[str]], *, font_size: int = 40) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for lines in pages:
        image = Image.new("L", (1600, 1000), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=font_size)
        y = 40
        for line in lines:
            draw.text((40, y), line, fill=0, font=font)
            y += int(font_size * 1.75)
        pdf.drawImage(ImageReader(image), 20, 300, width=550, height=343)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def mixed_pdf(native_lines: list[str], scanned_lines: list[str]) -> bytes:
    """Page 1 has a text layer, page 2 is image only."""
    writer = PdfWriter()
    from pypdf import PdfReader

    for data in (native_pdf([native_lines]), scanned_pdf([scanned_lines])):
        writer.add_page(PdfReader(io.BytesIO(data)).pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _write(writer: PdfWriter) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def declared_huge_image_pdf(width: int = 30000, height: int = 30000) -> bytes:
    """An image XObject that declares 900 million pixels but carries a few bytes of data."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    image = StreamObject()
    image.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(width), NameObject("/Height"): NumberObject(height),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    image._data = zlib.compress(b"\x00" * 1024)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})
    })
    return _write(writer)


def content_stream_bomb_pdf(megabytes_compressed: float = 1.0) -> bytes:
    """A page whose compressed content stream expands about a thousand-fold into drawing operators."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    chunk = b"BT /F1 12 Tf (A) Tj ET\n" * 4096
    compressor = zlib.compressobj(9)
    data = b""
    target = int(megabytes_compressed * 1024 * 1024)
    while len(data) < target:
        data += compressor.compress(chunk)
    data += compressor.flush()
    stream = StreamObject()
    stream.update({NameObject("/Filter"): NameObject("/FlateDecode")})
    stream._data = data
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    return _write(writer)


def deeply_nested_pdf(depth: int = 6000) -> bytes:
    """Hand-written bytes: a catalog carrying an array nested `depth` levels deep. (pypdf cannot even
    write such a file, which is exactly why a reader must not trust one to parse.)"""
    nested = b"[" * depth + b"1" + b"]" * depth
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R /Nest " + nested + b" >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R /Size 4 >>\n%%EOF\n"
    )
