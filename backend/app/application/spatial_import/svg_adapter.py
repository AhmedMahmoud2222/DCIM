# ruff: noqa: E501
"""Existing SVG sanitizer output -> SIR, so SVG candidates flow through the same classification, calibration
and review path as DXF/VSDX. SVG user units are pixels (no physical meaning) with a Y-down axis, so a
calibration is always required before anything can be accepted."""

from app.application.spatial_import.sir import SirDocument, SirEntity
from app.application.svg_sanitizer import SanitizeResult


def sir_from_svg(result: SanitizeResult) -> SirDocument:
    entities: list[SirEntity] = []
    for index, shape in enumerate(result.shapes):
        ref = f"svg:{index}"
        if shape.shape_type == "rect":
            width, height = shape.width or 0.0, shape.height or 0.0
            if width > 0 and height > 0:
                entities.append(
                    SirEntity(kind="rect", ref=ref, cx=shape.x + width / 2, cy=shape.y + height / 2, width=width, height=height)
                )
        elif shape.shape_type == "circle":
            entities.append(SirEntity(kind="circle", ref=ref, cx=shape.x, cy=shape.y, radius=shape.radius or 0.0))
        elif shape.shape_type == "text":
            entities.append(SirEntity(kind="text", ref=ref, cx=shape.x, cy=shape.y, text=shape.text))
    return SirDocument(
        source_format="svg", parser_name="svg_sanitizer", parser_version="1", source_units="px", units_trusted=False,
        y_axis="down", entities=entities, layers=[], warnings=[{"code": "svg_warning", "detail": w[:300]} for w in result.warnings[:50]],
        objects_discovered=result.objects_discovered, unsupported_object_count=result.unsupported_object_count,
    )
