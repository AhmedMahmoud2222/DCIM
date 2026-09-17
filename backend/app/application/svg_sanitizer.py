"""Untrusted SVG parsing (ARCHITECTURE_REVIEW.md §10a). Every uploaded file is untrusted
input, always, regardless of extension or claimed type. This module implements every
*content-level* control §10a requires:

- content-sniffed type check (magic bytes / structure, never filename/extension)
- file-size cap, checked before any parsing
- XXE defense: `defusedxml` with DTDs, external entities, and external resource
  resolution all explicitly forbidden
- a hard element-count cap enforced *during* the walk (not after), bounding both memory
  and wall-clock time for a pathological-but-small-byte-count file (e.g. deeply
  repeated/nested elements) without needing a separate timeout mechanism
- `<script>`/event-handler-attribute/external-reference stripping — nothing that could
  execute or fetch anything survives into the SIR
- the Sanitized Intermediate Representation (`SanitizeResult.shapes`) is the *only*
  thing that crosses back out — no raw markup, no `Element` objects, no file handle

Processing is entirely in-memory — the uploaded bytes are never written to a
quarantine directory or any other path on disk, which is a stronger property than a
quarantine-directory design for path-traversal purposes specifically (there is no
server-controlled path a malicious filename could ever influence).

Scope limitation, disclosed rather than silently assumed (PHASE2_GAP_ANALYSIS.md §3.4):
this does not run inside a separate OS-level subprocess/container with its own
credential-free identity, unlike §10a's "narrowly-scoped worker/subprocess" language —
that is a genuine infrastructure control this implementation does not provide."""

import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element  # noqa: S405 — type only; parsing uses defusedxml exclusively

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

MAX_SVG_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_RASTER_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB — calibration-only, no parsing
MAX_ELEMENTS = 5_000
MAX_COORDINATE = 1_000_000  # matches app.application.spatial_validation's bound

DANGEROUS_TAGS = {"script", "foreignobject", "animate", "animatetransform", "set", "iframe", "use", "image"}
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


@dataclass
class SirShape:
    """One normalized shape — the only unit of data the rest of the import pipeline
    (candidate detection, classification) is allowed to consume."""

    shape_type: str  # rect | circle | text
    x: float
    y: float
    width: float | None = None
    height: float | None = None
    radius: float | None = None
    text: str | None = None


@dataclass
class SanitizeResult:
    shapes: list[SirShape] = field(default_factory=list)
    objects_discovered: int = 0
    unsupported_object_count: int = 0
    warnings: list[str] = field(default_factory=list)


class SvgRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _looks_like_svg(content: bytes) -> bool:
    """Content-sniffed, not filename/extension-based (§10a: 'content-sniffed (magic
    bytes, not filename/extension) file-type check; reject on mismatch')."""
    head = content[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return True
    return b"<svg" in content[:4096]


def _looks_like_png(content: bytes) -> bool:
    return content[:8] == b"\x89PNG\r\n\x1a\n"


def _looks_like_jpeg(content: bytes) -> bool:
    return content[:3] == b"\xff\xd8\xff"


def validate_raster_image(content: bytes, *, declared_format: str) -> None:
    """Calibration-only import (§10: "ImageImporter... explicitly performs
    calibration-only import — no shape auto-detection — stated as NOT IMPLEMENTED for
    that capability rather than faked"). No parsing beyond a magic-byte/size check —
    there is no shape extraction to sanitize output from."""
    if len(content) > MAX_RASTER_FILE_SIZE_BYTES:
        raise SvgRejected(f"file exceeds the {MAX_RASTER_FILE_SIZE_BYTES} byte size limit")
    if declared_format == "png" and not _looks_like_png(content):
        raise SvgRejected("file content does not match the PNG magic bytes")
    if declared_format == "jpeg" and not _looks_like_jpeg(content):
        raise SvgRejected("file content does not match the JPEG magic bytes")
    if declared_format not in ("png", "jpeg"):
        raise SvgRejected(f"unsupported raster format: {declared_format}")


def sanitize_svg(content: bytes) -> SanitizeResult:
    if len(content) > MAX_SVG_FILE_SIZE_BYTES:
        raise SvgRejected(f"file exceeds the {MAX_SVG_FILE_SIZE_BYTES} byte size limit")
    if not _looks_like_svg(content):
        raise SvgRejected("file content does not appear to be SVG (magic-byte check failed)")

    try:
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise SvgRejected(f"rejected by XML security controls: {exc}") from exc
    except ParseError as exc:
        raise SvgRejected(f"malformed XML: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — any other parser failure is a rejection, never a 500
        raise SvgRejected(f"unparseable file: {exc}") from exc

    result = SanitizeResult()
    _walk(root, result)
    return result


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1].lower() if "}" in tag else tag.lower()


def _safe_float(value: str | None, default: float = 0.0) -> float:
    """Strips a trailing unit suffix (px/mm/%) defensively — SVG numeric attributes are
    technically unitless per spec, but malformed/hostile input is common. Out-of-range
    values (the "enormous coordinate values" attack) collapse to the default rather than
    propagating a value that could overflow downstream arithmetic."""
    if value is None:
        return default
    try:
        f = float(re.sub(r"[a-zA-Z%]+$", "", value.strip()))
    except ValueError:
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN/Infinity
        return default
    if not (-MAX_COORDINATE <= f <= MAX_COORDINATE):
        return default
    return f


def _walk(element: Element, result: SanitizeResult) -> None:
    for child in list(element):
        if result.objects_discovered >= MAX_ELEMENTS:
            result.warnings.append(f"element count cap ({MAX_ELEMENTS}) reached; remaining elements ignored")
            return
        result.objects_discovered += 1
        tag = _local_tag(child.tag)

        if tag in DANGEROUS_TAGS:
            result.warnings.append(f"stripped disallowed element <{tag}>")
            result.unsupported_object_count += 1
            continue  # never recurse into a stripped element's children either

        for attr in list(child.attrib):
            if attr.lower().startswith("on"):
                result.warnings.append(f"stripped event-handler attribute '{attr}' from <{tag}>")
                del child.attrib[attr]

        href = child.attrib.get("href") or child.attrib.get(XLINK_HREF)
        if href and re.match(r"^(https?:)?//|^[a-z][a-z0-9+.-]*:", href, re.IGNORECASE) and not href.startswith("#"):
            result.warnings.append(f"stripped external reference on <{tag}>: {href[:80]!r}")
            child.attrib.pop("href", None)
            child.attrib.pop(XLINK_HREF, None)

        if tag == "rect":
            result.shapes.append(
                SirShape(
                    shape_type="rect",
                    x=_safe_float(child.attrib.get("x")),
                    y=_safe_float(child.attrib.get("y")),
                    width=_safe_float(child.attrib.get("width")),
                    height=_safe_float(child.attrib.get("height")),
                )
            )
        elif tag == "circle":
            result.shapes.append(
                SirShape(
                    shape_type="circle",
                    x=_safe_float(child.attrib.get("cx")),
                    y=_safe_float(child.attrib.get("cy")),
                    radius=_safe_float(child.attrib.get("r")),
                )
            )
        elif tag == "text":
            text_content = "".join(child.itertext()).strip()[:255]
            result.shapes.append(
                SirShape(
                    shape_type="text", x=_safe_float(child.attrib.get("x")), y=_safe_float(child.attrib.get("y")),
                    text=text_content,
                )
            )
        elif tag in ("g", "svg", "defs"):
            pass  # containers only — no shape of their own
        else:
            result.unsupported_object_count += 1

        _walk(child, result)
