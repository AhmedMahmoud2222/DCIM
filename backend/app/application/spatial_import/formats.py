"""Format detection by content, independent of filename. Declared metadata (extension, Content-Type) is
advisory; where it names a *known* spatial format that disagrees with the detected content, the upload is
rejected (declared/detected mismatch). Unknown extensions are ignored, as before. Detection here is
magic-byte only: no parsing happens in the API process."""

import re

SPATIAL_FORMATS = ("svg", "png", "jpeg", "dxf", "vsdx")
_EXTENSION_FORMAT = {
    "svg": "svg", "png": "png", "jpg": "jpeg", "jpeg": "jpeg", "dxf": "dxf", "vsdx": "vsdx", "vsd": "vsd", "vsdm": "vsdx",
    "dwg": "dwg", "pdf": "pdf",
}
_CONTENT_TYPE_FORMAT = {
    "image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpeg",
    "image/vnd.dxf": "dxf", "application/dxf": "dxf", "application/x-dxf": "dxf", "image/x-dxf": "dxf",
    "application/vnd.ms-visio.drawing": "vsdx",
    "application/vnd.ms-visio.drawing.main+xml": "vsdx",
}
_BINARY_DXF = b"AutoCAD Binary DXF"
_DXF_START = re.compile(rb"\A\s*(?:999\r?\n[^\r\n]*\r?\n\s*)*0\r?\n\s*SECTION\r?\n\s*2\r?\n", re.IGNORECASE)


class UnsupportedFormat(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def detect_format(content: bytes) -> str:
    """Returns one of SPATIAL_FORMATS or raises UnsupportedFormat with a specific reason."""
    head = content[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if content[:4] == b"PK\x03\x04":
        return "vsdx"  # a ZIP container; the isolated parser proves it is (or is not) a Visio package
    if content[:18] == _BINARY_DXF:
        raise UnsupportedFormat("binary_dxf_unsupported", "Binary DXF is not supported; export ASCII DXF.")
    if content[:4] == b"AC10" or content[:6] in (b"AC1.40", b"AC1.50"):
        raise UnsupportedFormat("dwg_unsupported", "DWG is not supported; export DXF.")
    if content[:5] == b"%PDF-":
        raise UnsupportedFormat("pdf_not_approved", "PDF spatial import is not approved.")
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in content[:4096]:
        return "svg"
    if _DXF_START.match(content[:2048]):
        return "dxf"
    raise UnsupportedFormat(
        "unrecognized_content",
        "File content is not recognized as SVG, PNG, JPEG, DXF or VSDX (checked by content, not filename).",
    )


def declared_format(filename: str | None, content_type: str | None) -> set[str]:
    """Known formats the client claims via extension and Content-Type (empty when it claims nothing known)."""
    claimed: set[str] = set()
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[1].lower()
        if ext in _EXTENSION_FORMAT:
            claimed.add(_EXTENSION_FORMAT[ext])
    if content_type:
        mapped = _CONTENT_TYPE_FORMAT.get(content_type.split(";", 1)[0].strip().lower())
        if mapped:
            claimed.add(mapped)
    return claimed


def check_declared_matches(detected: str, filename: str | None, content_type: str | None) -> None:
    claimed = declared_format(filename, content_type)
    if claimed and detected not in claimed:
        raise UnsupportedFormat(
            "format_mismatch",
            f"File is declared as {'/'.join(sorted(claimed))} but its content is {detected}.",
        )
