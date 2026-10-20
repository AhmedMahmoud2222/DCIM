"""Hard resource limits for untrusted spatial-file parsing. Every bound is enforced twice: inside the
sandboxed parser child (so a hostile file cannot exhaust the child) and again by the parent when it
re-validates the SIR the child returned (so a compromised child cannot hand back an oversized or
malformed document)."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ParserLimits:
    # Container / input
    max_input_bytes: int = 20 * 1024 * 1024
    max_text_lines: int = 4_000_000  # DXF: 2 lines per group pair
    # Geometry
    max_entities: int = 50_000  # entities kept in the SIR
    max_expanded_entities: int = 100_000  # entities visited incl. block expansion
    max_block_depth: int = 8
    max_coordinate: float = 1.0e9  # source units; refuses overflow-style values
    max_text_length: int = 256
    max_layers: int = 500
    max_points_per_entity: int = 512
    max_warnings: int = 50
    # VSDX package
    max_zip_entries: int = 500
    max_entry_uncompressed_bytes: int = 20 * 1024 * 1024
    max_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_xml_elements: int = 400_000
    max_xml_depth: int = 64
    max_shape_depth: int = 24
    max_pages: int = 20
    # Child process
    cpu_seconds: int = 20
    wall_seconds: float = 40.0
    address_space_bytes: int = 1024 * 1024 * 1024
    max_sir_bytes: int = 24 * 1024 * 1024

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


DEFAULT_LIMITS = ParserLimits()
