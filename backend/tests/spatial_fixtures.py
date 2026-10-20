"""Deterministic, repo-owned spatial fixtures (Issue #104). Everything is generated from code: no CAD/Visio
software and no binary blobs are needed, and every hostile variant is built from the same primitives so the
test shows exactly which byte-level property it exercises."""

import io
import zipfile
from collections.abc import Iterable

MM_PER_IN = 25.4


# ------------------------------------------------------------------------------------------ DXF
def dxf(entities: str = "", *, insunits: int | None = 4, blocks: str = "", layers: Iterable[str] = (), eof: bool = True) -> bytes:
    header = ""
    if insunits is not None:
        header = f"0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n{insunits}\n0\nENDSEC\n"
    tables = ""
    if layers:
        rows = "".join(f"0\nLAYER\n2\n{name}\n70\n0\n" for name in layers)
        tables = f"0\nSECTION\n2\nTABLES\n{rows}0\nENDSEC\n"
    blocks_section = f"0\nSECTION\n2\nBLOCKS\n{blocks}0\nENDSEC\n" if blocks else ""
    body = f"{header}{tables}{blocks_section}0\nSECTION\n2\nENTITIES\n{entities}0\nENDSEC\n"
    return (body + ("0\nEOF\n" if eof else "")).encode()


def lwpoly(handle: str, layer: str, points: list[tuple[float, float]], closed: bool = True) -> str:
    verts = "".join(f"10\n{x}\n20\n{y}\n" for x, y in points)
    return f"0\nLWPOLYLINE\n5\n{handle}\n8\n{layer}\n90\n{len(points)}\n70\n{1 if closed else 0}\n{verts}"


def rect_poly(handle: str, layer: str, x: float, y: float, w: float, h: float) -> str:
    return lwpoly(handle, layer, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)])


def text(handle: str, layer: str, x: float, y: float, value: str, height: float = 100) -> str:
    return f"0\nTEXT\n5\n{handle}\n8\n{layer}\n10\n{x}\n20\n{y}\n40\n{height}\n1\n{value}\n"


def line(handle: str, layer: str, x1: float, y1: float, x2: float, y2: float) -> str:
    return f"0\nLINE\n5\n{handle}\n8\n{layer}\n10\n{x1}\n20\n{y1}\n11\n{x2}\n21\n{y2}\n"


def insert(handle: str, layer: str, block: str, x: float, y: float, rotation: float = 0, scale: float = 1.0) -> str:
    return f"0\nINSERT\n5\n{handle}\n8\n{layer}\n2\n{block}\n10\n{x}\n20\n{y}\n41\n{scale}\n42\n{scale}\n50\n{rotation}\n"


def block(name: str, entities: str, base: tuple[float, float] = (0, 0)) -> str:
    return f"0\nBLOCK\n2\n{name}\n10\n{base[0]}\n20\n{base[1]}\n{entities}0\nENDBLK\n"


def rack_row_dxf(count: int = 4, *, origin: tuple[float, float] = (1000, 1000), pitch: float = 700, labels: bool = True) -> bytes:
    """A 6000 x 4000 mm room outline and a row of 600 x 1000 mm racks (DXF millimetres, Y up)."""
    entities = rect_poly("100", "WALLS", 0, 0, 6000, 4000)
    for i in range(count):
        x = origin[0] + i * pitch
        entities += rect_poly(f"2{i:02d}", "RACKS", x, origin[1], 600, 1000)
        if labels:
            entities += text(f"3{i:02d}", "TEXT", x + 300, origin[1] + 500, f"RACK-{i + 1:02d}")
    return dxf(entities, layers=("WALLS", "RACKS", "TEXT"))


# ------------------------------------------------------------------------------------------ VSDX
_NS = "http://schemas.microsoft.com/office/visio/2012/main"
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
    '<Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>'
    "</Relationships>"
)


def vsdx_shape(shape_id: int, pin_x_mm: float, pin_y_mm: float, w_mm: float, h_mm: float, *, label: str | None = None,
               angle_deg: float = 0.0, name: str = "Rectangle", children: str = "", shape_type: str = "Shape") -> str:
    """Visio stores lengths in inches; dimensions here are millimetres for readability."""
    import math

    cells = (
        f'<Cell N="PinX" V="{pin_x_mm / MM_PER_IN}"/><Cell N="PinY" V="{pin_y_mm / MM_PER_IN}"/>'
        f'<Cell N="Width" V="{w_mm / MM_PER_IN}"/><Cell N="Height" V="{h_mm / MM_PER_IN}"/>'
        f'<Cell N="LocPinX" V="{w_mm / MM_PER_IN / 2}"/><Cell N="LocPinY" V="{h_mm / MM_PER_IN / 2}"/>'
        f'<Cell N="Angle" V="{math.radians(angle_deg)}"/>'
    )
    text_el = f"<Text>{label}</Text>" if label is not None else ""
    kids = f"<Shapes>{children}</Shapes>" if children else ""
    return f'<Shape ID="{shape_id}" Type="{shape_type}" NameU="{name}">{cells}{text_el}{kids}</Shape>'


def vsdx_page(shapes: str, *, page_scale: tuple[float, float] | None = None) -> str:
    sheet = ""
    if page_scale is not None:
        sheet = f'<PageSheet><Cell N="PageScale" V="{page_scale[0]}"/><Cell N="DrawingScale" V="{page_scale[1]}"/></PageSheet>'
    return f'<?xml version="1.0" encoding="UTF-8"?><PageContents xmlns="{_NS}">{sheet}<Shapes>{shapes}</Shapes></PageContents>'


def vsdx(shapes: str | None = None, *, extra: dict[str, bytes] | None = None, override: dict[str, bytes] | None = None,
         drop: Iterable[str] = (), page_scale: tuple[float, float] | None = None, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    parts: dict[str, bytes] = {
        "[Content_Types].xml": _CT.encode(),
        "_rels/.rels": _ROOT_RELS.encode(),
        "visio/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><VisioDocument xmlns="{_NS}"/>'.encode(),
        "visio/pages/page1.xml": vsdx_page(shapes or "", page_scale=page_scale).encode(),
    }
    parts.update(override or {})
    for name in drop:
        parts.pop(name, None)
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def rack_row_vsdx(count: int = 4, *, origin_mm: tuple[float, float] = (1000, 1000), pitch_mm: float = 700) -> bytes:
    shapes = vsdx_shape(1, 3000, 2000, 6000, 4000, name="Room")
    for i in range(count):
        shapes += vsdx_shape(10 + i, origin_mm[0] + i * pitch_mm + 300, origin_mm[1] + 500, 600, 1000, label=f"RACK-{i + 1:02d}")
    return vsdx(shapes)
