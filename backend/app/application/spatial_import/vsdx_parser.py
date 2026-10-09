# ruff: noqa: E501
"""VSDX (Visio) -> SIR. A .vsdx is an untrusted ZIP of XML parts; this module treats it as such and only
ever runs inside the sandboxed child (see worker.py).

Package controls, all enforced before any part is interpreted: entry-count cap; entry-name validation
(absolute paths, `..`, backslashes, drive letters, NUL, duplicates); encrypted-entry rejection; declared and
*actual* (streamed) expanded-size caps per entry and in total; a compression-ratio cap; no external
relationships and no relationship target escaping the package; no macro/ActiveX/OLE content; the required
Visio parts must exist. XML controls: defusedxml with DTDs, entities and external references forbidden, plus an
element-count and nesting cap enforced while parsing. Shape controls: iterative traversal with a group-depth
cap and a shape-count budget. Only geometry (Pin/Width/Height/Angle/LocPin), text and shape names are
extracted; embedded images (Foreign shapes), masters, themes, custom properties and all ForeignData are never
decoded."""

import io
import math
import posixpath
import re
import zipfile
from xml.etree.ElementTree import Element, TreeBuilder  # noqa: S405 - parsing uses defusedxml exclusively

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser, ParseError

from app.application.spatial_import.affine import Matrix, rotation, translation
from app.application.spatial_import.limits import DEFAULT_LIMITS, ParserLimits
from app.application.spatial_import.sir import SirDocument, SirEntity

PARSER_NAME = "vsdx_zip_xml"
PARSER_VERSION = "1"

_NS = "{http://schemas.microsoft.com/office/visio/2012/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_DRIVE = re.compile(r"^[A-Za-z]:")
_ACTIVE_NAMES = ("vbaproject", "activex", "macros/", "/macros", ".vbs", ".js", ".exe", ".dll")
_ACTIVE_REL_TYPES = ("vbaproject", "oleobject", "activex", "script", "macro")
_REQUIRED_PARTS = ("[Content_Types].xml", "visio/document.xml")


class VsdxRejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class _BoundedBuilder(TreeBuilder):
    def __init__(self, limits: ParserLimits) -> None:
        super().__init__()
        self._limits = limits
        self._depth = 0
        self._elements = 0

    def start(self, tag: str, attrs: dict[str, str]) -> Element:
        self._depth += 1
        self._elements += 1
        if self._depth > self._limits.max_xml_depth:
            raise VsdxRejected("vsdx_xml_too_deep", "XML nesting exceeds the depth limit")
        if self._elements > self._limits.max_xml_elements:
            raise VsdxRejected("vsdx_xml_too_large", "XML element count exceeds the limit")
        return super().start(tag, attrs)

    def end(self, tag: str) -> Element:
        element = super().end(tag)
        self._depth -= 1
        return element


def _parse_xml(data: bytes, limits: ParserLimits) -> Element:
    try:
        parser = DefusedXMLParser(
            target=_BoundedBuilder(limits), forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        parser.feed(data)
        return parser.close()
    except VsdxRejected:
        raise
    except DefusedXmlException:
        raise VsdxRejected("vsdx_xml_forbidden", "DTD, entity or external reference in XML") from None
    except (ParseError, ValueError):
        raise VsdxRejected("vsdx_xml_malformed", "XML part is not well formed") from None
    except RecursionError:
        raise VsdxRejected("vsdx_xml_too_deep", "XML nesting exceeds the depth limit") from None


def _check_name(name: str) -> None:
    if (
        not name or "\x00" in name or "\\" in name or name.startswith("/") or _DRIVE.match(name)
        or any(part == ".." for part in name.split("/"))
    ):
        raise VsdxRejected("vsdx_path_traversal", "package entry name is not a safe relative path")
    lowered = name.lower()
    if any(marker in lowered for marker in _ACTIVE_NAMES):
        raise VsdxRejected("vsdx_active_content", "package contains macro or active content")


class _Package:
    def __init__(self, content: bytes, limits: ParserLimits) -> None:
        self.limits = limits
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(content))
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            raise VsdxRejected("vsdx_malformed_package", "not a readable ZIP package") from None
        infos = self.zf.infolist()
        if len(infos) > limits.max_zip_entries:
            raise VsdxRejected("vsdx_too_many_entries", "package has too many entries")
        seen: set[str] = set()
        declared_total = 0
        for info in infos:
            _check_name(info.filename)
            if info.filename.endswith("/"):
                continue
            key = info.filename.lower()
            if key in seen:
                raise VsdxRejected("vsdx_duplicate_entry", "package has duplicate entry names")
            seen.add(key)
            if info.flag_bits & 0x1:
                raise VsdxRejected("vsdx_encrypted", "encrypted package entries are not supported")
            if info.file_size > limits.max_entry_uncompressed_bytes:
                raise VsdxRejected("vsdx_entry_too_large", "package entry expands beyond the limit")
            if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio:
                raise VsdxRejected("vsdx_compression_ratio", "package entry compression ratio is abusive")
            declared_total += info.file_size
        if declared_total > limits.max_total_uncompressed_bytes:
            raise VsdxRejected("vsdx_package_too_large", "package expands beyond the total limit")
        if declared_total > 1024 * 1024 and declared_total / max(len(content), 1) > limits.max_compression_ratio:
            raise VsdxRejected("vsdx_compression_ratio", "package compression ratio is abusive")
        self.names = {i.filename for i in infos if not i.filename.endswith("/")}
        self.actual_total = 0

    def read(self, name: str) -> bytes:
        info = self.zf.getinfo(name)
        budget = min(info.file_size, self.limits.max_entry_uncompressed_bytes)
        chunks: list[bytes] = []
        read = 0
        try:
            with self.zf.open(info) as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > budget:  # the header lied about the expanded size
                        raise VsdxRejected("vsdx_entry_too_large", "package entry expands beyond its declared size")
                    chunks.append(chunk)
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, zlib_error):
            raise VsdxRejected("vsdx_malformed_package", "package entry is corrupt") from None
        self.actual_total += read
        if self.actual_total > self.limits.max_total_uncompressed_bytes:
            raise VsdxRejected("vsdx_package_too_large", "package expands beyond the total limit")
        return b"".join(chunks)


try:  # zlib.error is what a corrupt deflate stream raises
    from zlib import error as zlib_error
except ImportError:  # pragma: no cover
    zlib_error = OSError  # type: ignore[misc,assignment]


def _validate_relationships(package: _Package) -> None:
    for name in sorted(n for n in package.names if n.endswith(".rels")):
        root = _parse_xml(package.read(name), package.limits)
        base = posixpath.dirname(posixpath.dirname(name))  # a part's rels live in <dir>/_rels/<part>.rels
        for rel in root.iter(f"{_REL_NS}Relationship"):
            if (rel.get("TargetMode") or "").lower() == "external":
                raise VsdxRejected("vsdx_external_relationship", "package declares an external relationship")
            rel_type = (rel.get("Type") or "").lower()
            if any(marker in rel_type for marker in _ACTIVE_REL_TYPES):
                raise VsdxRejected("vsdx_active_content", "package relationship points at active content")
            target = rel.get("Target") or ""
            if "://" in target or target.startswith("\\\\") or "\x00" in target:
                raise VsdxRejected("vsdx_external_relationship", "relationship target is not package-local")
            resolved = posixpath.normpath(posixpath.join(base, target)) if not target.startswith("/") else posixpath.normpath(target[1:])
            if resolved.startswith("..") or resolved.startswith("/"):
                raise VsdxRejected("vsdx_path_traversal", "relationship target escapes the package")


def _validate_content_types(package: _Package) -> None:
    root = _parse_xml(package.read("[Content_Types].xml"), package.limits)
    for element in root:
        content_type = (element.get("ContentType") or "").lower()
        if "macroenabled" in content_type or "vbaproject" in content_type or "activex" in content_type:
            raise VsdxRejected("vsdx_active_content", "macro-enabled Visio content is not accepted")
    if not any("visio" in (e.get("ContentType") or "").lower() for e in root):
        raise VsdxRejected("vsdx_not_visio", "package is not a Visio document")


def _page_parts(package: _Package) -> list[str]:
    pages = sorted(
        (n for n in package.names if re.fullmatch(r"visio/pages/page\d+\.xml", n)),
        key=lambda n: int(re.findall(r"\d+", n)[-1]),
    )
    if not pages:
        raise VsdxRejected("vsdx_missing_part", "no Visio page parts found")
    return pages[: package.limits.max_pages]


def _cell_map(shape: Element) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in shape.findall(f"{_NS}Cell"):
        name, value = cell.get("N"), cell.get("V")
        if name and value is not None and name not in cells:
            cells[name] = value
    return cells


def _f(cells: dict[str, str], name: str, limits: ParserLimits) -> float | None:
    raw = cells.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or abs(value) > limits.max_coordinate:
        raise VsdxRejected("vsdx_coordinate_out_of_range", "coordinate outside the permitted range")
    return value


def _text_of(shape: Element, limits: ParserLimits) -> str | None:
    node = shape.find(f"{_NS}Text")
    if node is None:
        return None
    text = " ".join("".join(node.itertext()).split())
    if not text:
        return None
    if len(text) > limits.max_text_length:
        raise VsdxRejected("vsdx_text_too_long", "shape text exceeds the maximum length")
    return text


def _drawing_scale(page_root: Element, limits: ParserLimits) -> float:
    """Real-world inches per page inch (DrawingScale / PageScale). 1.0 when undeclared or invalid."""
    sheet = page_root.find(f"{_NS}PageSheet")
    if sheet is None:
        return 1.0
    cells = _cell_map(sheet)
    page_scale, drawing_scale = _f(cells, "PageScale", limits), _f(cells, "DrawingScale", limits)
    if page_scale and drawing_scale and page_scale > 0 and drawing_scale > 0:
        ratio = drawing_scale / page_scale
        return ratio if 1e-4 <= ratio <= 1e4 else 1.0
    return 1.0


def parse_vsdx(content: bytes, *, limits: ParserLimits = DEFAULT_LIMITS) -> SirDocument:
    if len(content) > limits.max_input_bytes:
        raise VsdxRejected("vsdx_too_large", "file exceeds the VSDX size limit")
    package = _Package(content, limits)
    for part in _REQUIRED_PARTS:
        if part not in package.names:
            raise VsdxRejected("vsdx_missing_part", f"required part {part} is missing")
    _validate_content_types(package)
    _validate_relationships(package)

    warnings: list[dict[str, str]] = []
    entities: list[SirEntity] = []
    layers: list[str] = []
    discovered = unsupported = 0
    scale_notes: list[float] = []
    if any(n.startswith("visio/embeddings/") for n in package.names):
        warnings.append({"code": "vsdx_embeddings_ignored", "detail": "Embedded objects are present and were not read"})

    def warn(code: str, detail: str) -> None:
        if len(warnings) < limits.max_warnings and not any(w["code"] == code for w in warnings):
            warnings.append({"code": code, "detail": detail})

    for page_index, part in enumerate(_page_parts(package), start=1):
        root = _parse_xml(package.read(part), limits)
        ratio = _drawing_scale(root, limits)
        scale_notes.append(ratio)
        layer_name = f"page{page_index}"
        if len(layers) < limits.max_layers:
            layers.append(layer_name)
        shapes_root = root.find(f"{_NS}Shapes")
        if shapes_root is None:
            continue
        # iterative DFS: (shape element, parent->page matrix, depth, ref prefix)
        stack: list[tuple[Element, Matrix, int, str]] = [
            (s, Matrix(), 0, f"vsdx:p{page_index}/") for s in reversed(shapes_root.findall(f"{_NS}Shape"))
        ]
        visited = 0
        while stack:
            shape, parent_matrix, depth, prefix = stack.pop()
            visited += 1
            discovered += 1
            if visited > limits.max_expanded_entities:
                raise VsdxRejected("vsdx_too_many_shapes", "shape count exceeds the limit")
            if depth > limits.max_shape_depth:
                raise VsdxRejected("vsdx_shape_depth", "group nesting exceeds the depth limit")
            shape_id = shape.get("ID") or str(visited)
            ref = f"{prefix}{shape_id}"
            kind = (shape.get("Type") or "Shape").lower()
            name = (shape.get("NameU") or shape.get("Name") or None)
            if name is not None:
                name = name[: limits.max_text_length]
            cells = _cell_map(shape)
            width, height = _f(cells, "Width", limits), _f(cells, "Height", limits)
            pin_x, pin_y = _f(cells, "PinX", limits), _f(cells, "PinY", limits)
            if pin_x is None or pin_y is None:
                if kind != "group":
                    unsupported += 1
                    warn("vsdx_shape_without_geometry", "Some shapes have no explicit position (master-inherited) and were skipped")
                    continue
                pin_x = pin_y = 0.0
            loc_x = _f(cells, "LocPinX", limits)
            loc_y = _f(cells, "LocPinY", limits)
            angle = math.degrees(_f(cells, "Angle", limits) or 0.0)
            w = width or 0.0
            h = height or 0.0
            loc_x = w / 2 if loc_x is None else loc_x
            loc_y = h / 2 if loc_y is None else loc_y
            local_to_parent = translation(-loc_x, -loc_y).then(rotation(angle)).then(translation(pin_x, pin_y))
            local_to_page = local_to_parent.then(parent_matrix)
            children = shape.find(f"{_NS}Shapes")
            if kind == "foreign":
                unsupported += 1
                warn("vsdx_foreign_ignored", "Embedded images/objects (Foreign shapes) are ignored")
                continue
            if kind == "group" or children is not None:
                label = _text_of(shape, limits)
                if label is not None:
                    cx, cy = local_to_page.apply(w / 2, h / 2)
                    entities.append(
                        SirEntity(kind="text", ref=ref + "#t", layer=layer_name, cx=cx * ratio, cy=cy * ratio, text=label, name=name)
                    )
                if children is not None:
                    for child in reversed(children.findall(f"{_NS}Shape")):
                        stack.append((child, local_to_page, depth + 1, f"{ref}/"))
                continue
            if width and height and width > 0 and height > 0:
                cx, cy = local_to_page.apply(w / 2, h / 2)
                rot = _normalise_angle(local_to_page.rotation_deg())
                rw, rh = w * ratio, h * ratio
                if abs(rot - 90) < 1e-6:  # same physical rectangle as an axis-aligned one with swapped sides
                    rot, rw, rh = 0.0, rh, rw
                entities.append(
                    SirEntity(
                        kind="rect", ref=ref, layer=layer_name, cx=cx * ratio, cy=cy * ratio, width=rw, height=rh,
                        rotation_deg=rot, text=_text_of(shape, limits), name=name,
                    )
                )
            else:
                label = _text_of(shape, limits)
                if label is not None:
                    cx, cy = local_to_page.apply(0.0, 0.0)
                    entities.append(SirEntity(kind="text", ref=ref, layer=layer_name, cx=cx * ratio, cy=cy * ratio, text=label, name=name))
                else:
                    unsupported += 1
            if len(entities) > limits.max_entities:
                raise VsdxRejected("vsdx_too_many_shapes", "entity count exceeds the limit")

    declared = any(abs(r - 1.0) > 1e-9 for r in scale_notes)
    if not declared:
        warn("vsdx_scale_undeclared", "No drawing scale found; values are Visio internal inches. Confirm the scale by calibration")
    return SirDocument(
        source_format="vsdx", parser_name=PARSER_NAME, parser_version=PARSER_VERSION, source_units="in",
        units_trusted=True, y_axis="up", entities=entities, layers=layers, warnings=warnings,
        objects_discovered=discovered, unsupported_object_count=unsupported,
        notes={"drawing_scale_declared": declared, "page_scale_ratios": [round(r, 6) for r in scale_notes]},
    )


def _normalise_angle(deg: float) -> float:
    deg = ((deg + 180.0) % 360.0) - 180.0
    if deg > 90:
        deg -= 180
    elif deg <= -90:
        deg += 180
    return 0.0 if abs(deg) < 1e-9 else deg
