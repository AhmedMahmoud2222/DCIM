"""Build, validate, install and roll back the offline DCIM basemap (country boundaries).

The dashboard draws country polygons from a locally packaged, version-pinned file; nothing is
fetched from the internet at runtime and there is no upload endpoint. Operators replace the
optional override through the normal deployment process with this tool. Python stdlib only.

    python scripts/dcim_basemap.py build --geojson ne_50m_admin_0_countries.geojson \\
        --out-dir out --version 5.1.2-r1
    python scripts/dcim_basemap.py validate out
    python scripts/dcim_basemap.py install out --target frontend/public/map-data
    python scripts/dcim_basemap.py status --target frontend/public/map-data
    python scripts/dcim_basemap.py rollback --target frontend/public/map-data

Exit codes: 0 success, 1 validation or operation failure (nothing on disk is changed), 2 usage.
See docs/OFFLINE_MAP_DATA.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DATA_FORMAT = "dcim-basemap"
MANIFEST_FORMAT = "dcim-basemap-manifest"
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
PRECISION = 100
PROJECTION = "equirectangular"
MAX_COUNTRIES = 400
MAX_POINTS = 250_000
MAX_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_NAME_LENGTH = 128
MAX_ID_LENGTH = 64
LON_LIMIT = 180 * PRECISION
LAT_LIMIT = 90 * PRECISION

MANIFEST_NAME = "manifest.json"
PREVIOUS_MANIFEST_NAME = "manifest.previous.json"
ROLLEDBACK_MANIFEST_NAME = "manifest.rolledback.json"

FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.json$")
ISO2_RE = re.compile(r"^[A-Z]{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_DATASET_ID = "natural-earth-admin0-50m"
DEFAULT_SOURCE_NAME = "Natural Earth"
DEFAULT_SOURCE_URL = "https://github.com/nvkelso/natural-earth-vector/tree/v5.1.2"
DEFAULT_SOURCE_LICENSE = "Public domain"
DEFAULT_TOLERANCE_DEGREES = 0.03
DEFAULT_MIN_AREA_SQ_DEGREES = 0.0005


class BasemapError(Exception):
    """A validation or operation failure with an operator-readable reason."""


# --------------------------------------------------------------------------- helpers


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_file_name(name: object, what: str = "file") -> str:
    if not isinstance(name, str) or not FILE_RE.fullmatch(name) or ".." in name:
        raise BasemapError(f"{what} {name!r} must match {FILE_RE.pattern} and contain no '..' or path separator")
    return name


def check_dir_argument(path: Path, what: str) -> Path:
    if ".." in path.parts:
        raise BasemapError(f"{what} {str(path)!r} must not contain '..' (path traversal refused)")
    return path


def atomic_write(path: Path, data: bytes) -> None:
    """Writes `data` to a temp file in the same directory, then os.replace()s it into place."""
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# --------------------------------------------------------------------------- validation


def _check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        raise BasemapError(f"{field} must be a non-empty string of at most {MAX_ID_LENGTH} characters")
    return value


def _check_schema_version(value: object, what: str) -> None:
    if not is_int(value) or value not in SUPPORTED_SCHEMA_VERSIONS:
        raise BasemapError(f"incompatible {what} schema_version {value!r} (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})")


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise BasemapError("manifest must be a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise BasemapError(f"manifest format must be {MANIFEST_FORMAT!r}, got {manifest.get('format')!r}")
    _check_schema_version(manifest.get("schema_version"), "manifest")
    _check_id(manifest.get("dataset_id"), "manifest dataset_id")
    _check_id(manifest.get("version"), "manifest version")
    check_file_name(manifest.get("file"), "manifest file")
    size = manifest.get("bytes")
    if not is_int(size) or not 0 < size <= MAX_BYTES:
        raise BasemapError(f"manifest bytes must be an integer in 1..{MAX_BYTES}, got {size!r}")
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise BasemapError("manifest sha256 must be 64 lowercase hex characters")
    if "created_at" in manifest and not isinstance(manifest["created_at"], str):
        raise BasemapError("manifest created_at must be a string")
    if "source" in manifest and not isinstance(manifest["source"], dict):
        raise BasemapError("manifest source must be an object")
    return manifest


def validate_data(data: object) -> dict:
    """Validates a parsed basemap data document; returns counts for reporting."""
    if not isinstance(data, dict):
        raise BasemapError("data file must be a JSON object")
    if data.get("format") != DATA_FORMAT:
        raise BasemapError(f"data format must be {DATA_FORMAT!r}, got {data.get('format')!r}")
    _check_schema_version(data.get("schema_version"), "data")
    _check_id(data.get("dataset_id"), "data dataset_id")
    _check_id(data.get("version"), "data version")
    if data.get("projection") != PROJECTION:
        raise BasemapError(f"data projection must be {PROJECTION!r}")
    if not is_int(data.get("precision")) or data.get("precision") != PRECISION:
        raise BasemapError(f"data precision must be {PRECISION}")
    countries = data.get("countries")
    if not isinstance(countries, list):
        raise BasemapError("data countries must be a list")
    if not countries:
        # The browser rejects an empty basemap, so a map that validates here must not be empty either.
        raise BasemapError("data countries must not be empty")
    if len(countries) > MAX_COUNTRIES:
        raise BasemapError(f"too many countries: {len(countries)} > {MAX_COUNTRIES}")
    seen_iso2: set[str] = set()
    points = 0
    for index, country in enumerate(countries):
        where = f"countries[{index}]"
        if not isinstance(country, dict):
            raise BasemapError(f"{where} must be an object")
        iso2 = country.get("iso2")
        if iso2 is not None:
            if not isinstance(iso2, str) or not ISO2_RE.fullmatch(iso2):
                raise BasemapError(f"{where}.iso2 {iso2!r} must match {ISO2_RE.pattern} or be null")
            if iso2 in seen_iso2:
                raise BasemapError(f"duplicate iso2 {iso2!r} at {where}")
            seen_iso2.add(iso2)
        name = country.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
            raise BasemapError(f"{where}.name must be a non-empty string of at most {MAX_NAME_LENGTH} characters")
        polygons = country.get("polygons")
        if not isinstance(polygons, list):
            raise BasemapError(f"{where}.polygons must be a list")
        for p_index, polygon in enumerate(polygons):
            if not isinstance(polygon, list) or not polygon:
                raise BasemapError(f"{where}.polygons[{p_index}] must be a non-empty list of rings")
            for r_index, ring in enumerate(polygon):
                ring_where = f"{where}.polygons[{p_index}][{r_index}]"
                if not isinstance(ring, list):
                    raise BasemapError(f"{ring_where} must be a flat list of integers")
                if len(ring) % 2:
                    raise BasemapError(f"{ring_where} has odd length {len(ring)}")
                if len(ring) < 6:
                    raise BasemapError(f"{ring_where} has fewer than 3 points")
                for v_index, value in enumerate(ring):
                    if not is_int(value):
                        raise BasemapError(f"{ring_where}[{v_index}] is not an integer: {value!r}")
                    limit = LON_LIMIT if v_index % 2 == 0 else LAT_LIMIT
                    if not -limit <= value <= limit:
                        axis = "longitude" if v_index % 2 == 0 else "latitude"
                        raise BasemapError(f"{ring_where}[{v_index}] {axis} {value} out of range +/-{limit}")
                points += len(ring) // 2
                if points > MAX_POINTS:
                    raise BasemapError(f"too many points: more than {MAX_POINTS}")
    return {"countries": len(countries), "points": points, "iso2": len(seen_iso2)}


def _strict_json(raw: bytes, what: str) -> object:
    def reject_constant(token: str) -> object:
        raise BasemapError(f"{what} contains non-finite number {token}")

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except UnicodeDecodeError as exc:
        raise BasemapError(f"{what} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BasemapError(f"{what} is not valid JSON (corrupt or truncated): {exc}") from exc


def validate_dir(directory: Path, manifest_name: str = MANIFEST_NAME) -> dict:
    """Validates `<directory>/<manifest_name>` and the data file it names. Returns a summary."""
    check_file_name(manifest_name, "manifest name")
    manifest_path = directory / manifest_name
    if not manifest_path.is_file():
        raise BasemapError(f"missing manifest {manifest_path}")
    manifest_raw = manifest_path.read_bytes()
    if len(manifest_raw) > MAX_MANIFEST_BYTES:
        raise BasemapError(f"manifest larger than {MAX_MANIFEST_BYTES} bytes")
    manifest = validate_manifest(_strict_json(manifest_raw, "manifest"))
    data_path = directory / manifest["file"]
    if not data_path.is_file():
        raise BasemapError(f"missing data file {data_path}")
    raw = data_path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise BasemapError(f"data file larger than {MAX_BYTES} bytes")
    if len(raw) != manifest["bytes"]:
        raise BasemapError(f"size mismatch: data file has {len(raw)} bytes, manifest says {manifest['bytes']}")
    digest = sha256_hex(raw)
    if digest != manifest["sha256"]:
        raise BasemapError(f"checksum mismatch: data file sha256 {digest}, manifest says {manifest['sha256']}")
    data = _strict_json(raw, "data file")
    counts = validate_data(data)
    if data["dataset_id"] != manifest["dataset_id"] or data["version"] != manifest["version"]:
        raise BasemapError(
            f"dataset/version mismatch: data is {data['dataset_id']} {data['version']}, "
            f"manifest is {manifest['dataset_id']} {manifest['version']}"
        )
    return {
        "dataset_id": manifest["dataset_id"],
        "version": manifest["version"],
        "file": manifest["file"],
        "bytes": len(raw),
        "sha256": digest,
        **counts,
    }


# --------------------------------------------------------------------------- build


def _round(value: float) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise BasemapError(f"source coordinate is not a finite number: {value!r}")
    return math.floor(value * PRECISION + 0.5)


def _segment_distance_sq(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    px, py = p[0] - ax, p[1] - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return float(px * px + py * py)
    t = max(0.0, min(1.0, (px * dx + py * dy) / length_sq))
    ex, ey = px - t * dx, py - t * dy
    return ex * ex + ey * ey


def _douglas_peucker_closed(points: list[tuple[int, int]], tolerance: float) -> list[tuple[int, int]]:
    """Simplifies a closed ring (first point not repeated); iterative, deterministic."""
    n = len(points)
    if n <= 4 or tolerance <= 0:
        return list(points)
    far = max(range(1, n), key=lambda i: ((points[i][0] - points[0][0]) ** 2 + (points[i][1] - points[0][1]) ** 2, -i))
    ext = points + [points[0]]
    keep = [False] * len(ext)
    keep[0] = keep[far] = keep[-1] = True
    tol_sq = tolerance * tolerance
    stack = [(0, far), (far, len(ext) - 1)]
    while stack:
        start, end = stack.pop()
        best, best_index = -1.0, -1
        for i in range(start + 1, end):
            d = _segment_distance_sq(ext[i], ext[start], ext[end])
            if d > best:
                best, best_index = d, i
        if best_index >= 0 and best > tol_sq:
            keep[best_index] = True
            stack.append((start, best_index))
            stack.append((best_index, end))
    return [ext[i] for i in range(n) if keep[i]]


def _area(points: list[tuple[int, int]]) -> float:
    """Absolute shoelace area in square degrees."""
    total = 0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2 / (PRECISION * PRECISION)


def _prepare_ring(raw_ring: object, tolerance: float) -> list[tuple[int, int]] | None:
    if not isinstance(raw_ring, list):
        raise BasemapError("source ring must be a list of positions")
    points: list[tuple[int, int]] = []
    for position in raw_ring:
        if not isinstance(position, list) or len(position) < 2:
            raise BasemapError("source position must be [lon, lat]")
        point = (_round(position[0]), _round(position[1]))
        if not points or points[-1] != point:
            points.append(point)
    while len(points) > 1 and points[-1] == points[0]:
        points.pop()
    if len(points) < 3:
        return None
    simplified = _douglas_peucker_closed(points, tolerance)
    if len(simplified) < 4:
        # Never reduce a ring below 4 points: keep evenly spaced original vertices instead.
        simplified = [points[(i * len(points)) // 4] for i in range(4)] if len(points) >= 4 else points
    return simplified


def _flatten(points: list[tuple[int, int]]) -> list[int]:
    return [value for point in points for value in point]


def _convert_geometry(geometry: object, tolerance: float, min_area: float) -> list[list[list[int]]]:
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon":
        raw_polygons = [coordinates]
    elif kind == "MultiPolygon":
        raw_polygons = coordinates
    else:
        return []
    if not isinstance(raw_polygons, list):
        raise BasemapError("source geometry coordinates must be a list")
    candidates: list[tuple[float, list[list[tuple[int, int]]]]] = []
    for raw_polygon in raw_polygons:
        if not isinstance(raw_polygon, list) or not raw_polygon:
            continue
        outer = _prepare_ring(raw_polygon[0], tolerance)
        if outer is None:
            continue
        holes = []
        for raw_hole in raw_polygon[1:]:
            hole = _prepare_ring(raw_hole, tolerance)
            if hole is not None and _area(hole) >= min_area:
                holes.append(hole)
        candidates.append((_area(outer), [outer, *holes]))
    if not candidates:
        return []
    kept = [rings for area, rings in candidates if area >= min_area]
    if not kept:
        # Never delete a tiny nation: keep its largest polygon even when it is below the area floor.
        kept = [max(candidates, key=lambda item: item[0])[1]]
    return [[_flatten(ring) for ring in rings] for rings in kept]


def _iso2(properties: dict) -> str | None:
    for key in ("ISO_A2_EH", "ISO_A2"):
        value = properties.get(key)
        if isinstance(value, str) and ISO2_RE.fullmatch(value):
            return value
    return None


def build_data(source: object, dataset_id: str, version: str, tolerance: float, min_area: float) -> bytes:
    if not isinstance(source, dict) or source.get("type") != "FeatureCollection":
        raise BasemapError("source must be a GeoJSON FeatureCollection")
    features = source.get("features")
    if not isinstance(features, list):
        raise BasemapError("source features must be a list")
    countries = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        name = properties.get("NAME") or properties.get("ADMIN")
        if not isinstance(name, str) or not name.strip():
            raise BasemapError("source feature has no NAME or ADMIN property")
        polygons = _convert_geometry(feature.get("geometry"), tolerance * PRECISION, min_area)
        if polygons:
            iso2 = _iso2(properties)
            countries.append(
                {
                    "iso2": iso2,
                    "name": name.strip()[:MAX_NAME_LENGTH],
                    "polygons": polygons,
                    "_direct": iso2 is not None and properties.get("ISO_A2") == iso2,
                }
            )
    # ISO_A2_EH repeats the parent code on some dependencies (e.g. Ashmore and Cartier Islands -> AU).
    # The feature whose own ISO_A2 carries the code keeps it; the others are drawn with iso2 null.
    by_code: dict[str, list[dict]] = {}
    for country in countries:
        if country["iso2"] is not None:
            by_code.setdefault(country["iso2"], []).append(country)
    for code, holders in by_code.items():
        if len(holders) > 1:
            direct = [c for c in holders if c.pop("_direct")]
            if len(direct) != 1:
                raise BasemapError(f"source has ambiguous iso2 {code!r} ({', '.join(c['name'] for c in holders)})")
            for country in holders:
                if country is not direct[0]:
                    country["iso2"] = None
    for country in countries:
        country.pop("_direct", None)
    countries.sort(key=lambda c: (c["name"], c["iso2"] or ""))
    document = {
        "format": DATA_FORMAT,
        "schema_version": 1,
        "dataset_id": dataset_id,
        "version": version,
        "projection": PROJECTION,
        "precision": PRECISION,
        "countries": countries,
    }
    validate_data(document)
    raw = (json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise BasemapError(f"built data file is {len(raw)} bytes, above the {MAX_BYTES} limit; raise --tolerance")
    return raw


def build_manifest(raw: bytes, file_name: str, dataset_id: str, version: str, created_at: str, source: dict) -> bytes:
    manifest = {
        "format": MANIFEST_FORMAT,
        "schema_version": 1,
        "dataset_id": dataset_id,
        "version": version,
        "file": file_name,
        "bytes": len(raw),
        "sha256": sha256_hex(raw),
        "created_at": created_at,
        "source": source,
    }
    validate_manifest(manifest)
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- commands


def cmd_build(args: argparse.Namespace) -> int:
    _check_id(args.version, "--version")
    _check_id(args.dataset_id, "--dataset-id")
    file_name = check_file_name(args.file or f"basemap-{args.version}.json", "--file")
    manifest_name = check_file_name(args.manifest_name, "--manifest-name")
    if file_name == manifest_name:
        raise BasemapError("--file and --manifest-name must differ")
    if not args.tolerance >= 0 or not args.min_area >= 0:
        raise BasemapError("--tolerance and --min-area must be >= 0")
    source_raw = Path(args.geojson).read_bytes()
    source = _strict_json(source_raw, "source GeoJSON")
    raw = build_data(source, args.dataset_id, args.version, args.tolerance, args.min_area)
    created_at = args.created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_info = {
        "name": args.source_name,
        "url": args.source_url,
        "license": args.source_license,
        "source_sha256": sha256_hex(source_raw),
    }
    manifest = build_manifest(raw, file_name, args.dataset_id, args.version, created_at, source_info)
    out_dir = check_dir_argument(Path(args.out_dir), "--out-dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / file_name, raw)
    atomic_write(out_dir / manifest_name, manifest)
    summary = validate_dir(out_dir, manifest_name)
    print(
        f"OK: built {summary['dataset_id']} {summary['version']} -> {out_dir / file_name} "
        f"({summary['bytes']} bytes, {summary['countries']} countries, {summary['points']} points, "
        f"sha256 {summary['sha256']})"
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    summary = validate_dir(Path(args.dir), args.manifest)
    print(
        f"OK: {summary['dataset_id']} {summary['version']} file={summary['file']} bytes={summary['bytes']} "
        f"countries={summary['countries']} points={summary['points']} sha256={summary['sha256']}"
    )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    source_dir = Path(args.dir)
    target = check_dir_argument(Path(args.target), "--target")
    summary = validate_dir(source_dir, MANIFEST_NAME)
    if target.exists() and not target.is_dir():
        raise BasemapError(f"--target {target} is not a directory")
    if target.is_dir() and target.resolve() == source_dir.resolve():
        raise BasemapError("--target must differ from the source directory")
    data_raw = (source_dir / summary["file"]).read_bytes()
    manifest_raw = (source_dir / MANIFEST_NAME).read_bytes()
    target_data = target / summary["file"]
    if target_data.exists() and (not target_data.is_file() or target_data.read_bytes() != data_raw):
        raise BasemapError(
            f"{target_data} already exists with different content; data files are immutable, use a new version/file name"
        )
    target.mkdir(parents=True, exist_ok=True)
    if not target_data.exists():
        atomic_write(target_data, data_raw)
    active = target / MANIFEST_NAME
    if active.is_file():
        current = active.read_bytes()
        if current != manifest_raw:
            atomic_write(target / PREVIOUS_MANIFEST_NAME, current)
    atomic_write(active, manifest_raw)
    validate_dir(target, MANIFEST_NAME)
    print(f"OK: installed {summary['dataset_id']} {summary['version']} into {target}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    target = check_dir_argument(Path(args.target), "--target")
    previous = target / PREVIOUS_MANIFEST_NAME
    if not previous.is_file():
        raise BasemapError(f"no previous manifest to roll back to ({previous} does not exist)")
    try:
        summary = validate_dir(target, PREVIOUS_MANIFEST_NAME)
    except BasemapError as exc:
        raise BasemapError(f"previous manifest is not valid, refusing to roll back: {exc}") from exc
    active = target / MANIFEST_NAME
    if active.is_file():
        atomic_write(target / ROLLEDBACK_MANIFEST_NAME, active.read_bytes())
    os.replace(previous, active)
    print(f"OK: rolled back {target} to {summary['dataset_id']} {summary['version']}")
    return 0


def _describe(target: Path, manifest_name: str) -> tuple[str, bool]:
    if not (target / manifest_name).is_file():
        return "none", True
    try:
        summary = validate_dir(target, manifest_name)
    except BasemapError as exc:
        return f"INVALID ({exc})", False
    return f"{summary['dataset_id']} {summary['version']} ({summary['file']}, valid)", True


def cmd_status(args: argparse.Namespace) -> int:
    target = check_dir_argument(Path(args.target), "--target")
    active, active_ok = _describe(target, MANIFEST_NAME)
    previous, _ = _describe(target, PREVIOUS_MANIFEST_NAME)
    if active == "none":
        active = "none (the browser uses the bundled basemap)"
    print(f"active:   {active}")
    print(f"previous: {previous}")
    return 0 if active_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="convert Natural Earth admin-0 GeoJSON into a data file and manifest")
    build.add_argument("--geojson", required=True, help="source GeoJSON FeatureCollection (Polygon/MultiPolygon)")
    build.add_argument("--out-dir", required=True)
    build.add_argument("--version", required=True, help="dataset version, e.g. 5.1.2-r1")
    build.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    build.add_argument("--file", help="data file name (default basemap-<version>.json)")
    build.add_argument("--manifest-name", default=MANIFEST_NAME)
    build.add_argument("--source-name", default=DEFAULT_SOURCE_NAME)
    build.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    build.add_argument("--source-license", default=DEFAULT_SOURCE_LICENSE)
    build.add_argument("--created-at", help="manifest timestamp (default: now, UTC)")
    build.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_DEGREES, help="simplification, degrees")
    build.add_argument("--min-area", type=float, default=DEFAULT_MIN_AREA_SQ_DEGREES, help="drop smaller islands, deg^2")
    build.set_defaults(func=cmd_build)

    validate = sub.add_parser("validate", help="validate a directory's manifest and data file")
    validate.add_argument("dir")
    validate.add_argument("--manifest", default=MANIFEST_NAME, help="manifest file name inside dir")
    validate.set_defaults(func=cmd_validate)

    install = sub.add_parser("install", help="validate <dir>, then install it into --target atomically")
    install.add_argument("dir")
    install.add_argument("--target", required=True, help="map-data directory served at /map-data/")
    install.set_defaults(func=cmd_install)

    rollback = sub.add_parser("rollback", help="restore manifest.previous.json atomically")
    rollback.add_argument("--target", required=True)
    rollback.set_defaults(func=cmd_rollback)

    status = sub.add_parser("status", help="report the active and previous versions")
    status.add_argument("--target", required=True)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BasemapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
