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
import contextlib
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
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


# --------------------------------------------------------------------------- validation


def _check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        raise BasemapError(f"{field} must be a non-empty string of at most {MAX_ID_LENGTH} characters")
    return value


def _check_schema_version(value: object, what: str) -> None:
    if not is_int(value) or value not in SUPPORTED_SCHEMA_VERSIONS:
        raise BasemapError(f"incompatible {what} schema_version {value!r} (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})")


def _check_header(doc: object, fmt: str, what: str, noun: str) -> dict:
    """Checks what manifests and data files share: object, format, schema_version, dataset_id, version."""
    if not isinstance(doc, dict):
        raise BasemapError(f"{noun} must be a JSON object")
    if doc.get("format") != fmt:
        raise BasemapError(f"{what} format must be {fmt!r}, got {doc.get('format')!r}")
    _check_schema_version(doc.get("schema_version"), what)
    _check_id(doc.get("dataset_id"), f"{what} dataset_id")
    _check_id(doc.get("version"), f"{what} version")
    return doc


def validate_manifest(manifest: object) -> dict:
    manifest = _check_header(manifest, MANIFEST_FORMAT, "manifest", "manifest")
    check_file_name(manifest.get("file"), "manifest file")
    if not is_int(size := manifest.get("bytes")) or not 0 < size <= MAX_BYTES:
        raise BasemapError(f"manifest bytes must be an integer in 1..{MAX_BYTES}, got {size!r}")
    if not isinstance(digest := manifest.get("sha256"), str) or not SHA256_RE.fullmatch(digest):
        raise BasemapError("manifest sha256 must be 64 lowercase hex characters")
    for key, kind, expected in (("created_at", str, "a string"), ("source", dict, "an object")):
        if key in manifest and not isinstance(manifest[key], kind):
            raise BasemapError(f"manifest {key} must be {expected}")
    return manifest


def _check_ring(ring: object, where: str) -> int:
    """Validates one flat [lon, lat, ...] ring; returns its point count."""
    if not isinstance(ring, list):
        raise BasemapError(f"{where} must be a flat list of integers")
    if len(ring) % 2:
        raise BasemapError(f"{where} has odd length {len(ring)}")
    if len(ring) < 6:
        raise BasemapError(f"{where} has fewer than 3 points")
    for v_index, value in enumerate(ring):
        if not is_int(value):
            raise BasemapError(f"{where}[{v_index}] is not an integer: {value!r}")
        limit, axis = (LON_LIMIT, "longitude") if v_index % 2 == 0 else (LAT_LIMIT, "latitude")
        if not -limit <= value <= limit:
            raise BasemapError(f"{where}[{v_index}] {axis} {value} out of range +/-{limit}")
    return len(ring) // 2


def _check_polygons(polygons: object, where: str, points: int) -> int:
    """Validates one country's polygons; returns the running point total."""
    if not isinstance(polygons, list):
        raise BasemapError(f"{where}.polygons must be a list")
    for p_index, polygon in enumerate(polygons):
        if not isinstance(polygon, list) or not polygon:
            raise BasemapError(f"{where}.polygons[{p_index}] must be a non-empty list of rings")
        for r_index, ring in enumerate(polygon):
            points += _check_ring(ring, f"{where}.polygons[{p_index}][{r_index}]")
            if points > MAX_POINTS:
                raise BasemapError(f"too many points: more than {MAX_POINTS}")
    return points


def _check_country(country: object, where: str, seen_iso2: set[str], points: int) -> int:
    """Validates one country (iso2 format and uniqueness, name, polygons); returns the running point total."""
    if not isinstance(country, dict):
        raise BasemapError(f"{where} must be an object")
    if (iso2 := country.get("iso2")) is not None:
        if not isinstance(iso2, str) or not ISO2_RE.fullmatch(iso2):
            raise BasemapError(f"{where}.iso2 {iso2!r} must match {ISO2_RE.pattern} or be null")
        if iso2 in seen_iso2:
            raise BasemapError(f"duplicate iso2 {iso2!r} at {where}")
        seen_iso2.add(iso2)
    name = country.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        raise BasemapError(f"{where}.name must be a non-empty string of at most {MAX_NAME_LENGTH} characters")
    return _check_polygons(country.get("polygons"), where, points)


def validate_data(data: object) -> dict:
    """Validates a parsed basemap data document; returns counts for reporting."""
    data = _check_header(data, DATA_FORMAT, "data", "data file")
    if data.get("projection") != PROJECTION:
        raise BasemapError(f"data projection must be {PROJECTION!r}")
    if not is_int(data.get("precision")) or data.get("precision") != PRECISION:
        raise BasemapError(f"data precision must be {PRECISION}")
    if not isinstance(countries := data.get("countries"), list):
        raise BasemapError("data countries must be a list")
    if not countries:
        # The browser rejects an empty basemap, so a map that validates here must not be empty either.
        raise BasemapError("data countries must not be empty")
    if len(countries) > MAX_COUNTRIES:
        raise BasemapError(f"too many countries: {len(countries)} > {MAX_COUNTRIES}")
    seen_iso2, points = set(), 0
    for index, country in enumerate(countries):
        points = _check_country(country, f"countries[{index}]", seen_iso2, points)
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


def _read_file(path: Path, what: str, limit: int) -> bytes:
    if not path.is_file():
        raise BasemapError(f"missing {what} {path}")
    if len(raw := path.read_bytes()) > limit:
        raise BasemapError(f"{what} larger than {limit} bytes")
    return raw


def validate_dir(directory: Path, manifest_name: str = MANIFEST_NAME) -> dict:
    """Validates `<directory>/<manifest_name>` and the data file it names. Returns a summary."""
    check_file_name(manifest_name, "manifest name")
    manifest_raw = _read_file(directory / manifest_name, "manifest", MAX_MANIFEST_BYTES)
    manifest = validate_manifest(_strict_json(manifest_raw, "manifest"))
    raw = _read_file(directory / manifest["file"], "data file", MAX_BYTES)
    if len(raw) != manifest["bytes"]:
        raise BasemapError(f"size mismatch: data file has {len(raw)} bytes, manifest says {manifest['bytes']}")
    if (digest := sha256_hex(raw)) != manifest["sha256"]:
        raise BasemapError(f"checksum mismatch: data file sha256 {digest}, manifest says {manifest['sha256']}")
    data = _strict_json(raw, "data file")
    counts = validate_data(data)
    if data["dataset_id"] != manifest["dataset_id"] or data["version"] != manifest["version"]:
        raise BasemapError(
            f"dataset/version mismatch: data is {data['dataset_id']} {data['version']}, "
            f"manifest is {manifest['dataset_id']} {manifest['version']}"
        )
    return {**{key: manifest[key] for key in ("dataset_id", "version", "file")}, "bytes": len(raw), "sha256": digest, **counts}


# --------------------------------------------------------------------------- build


def _round(value: float) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise BasemapError(f"source coordinate is not a finite number: {value!r}")
    return math.floor(value * PRECISION + 0.5)


def _segment_distance_sq(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> float:
    dx, dy, px, py = b[0] - a[0], b[1] - a[1], p[0] - a[0], p[1] - a[1]
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
    stack = [(0, far), (far, len(ext) - 1)]
    while stack:
        start, end = stack.pop()
        best, best_index = -1.0, -1
        for i in range(start + 1, end):
            d = _segment_distance_sq(ext[i], ext[start], ext[end])
            if d > best:
                best, best_index = d, i
        if best_index >= 0 and best > tolerance * tolerance:
            keep[best_index] = True
            stack.extend(((start, best_index), (best_index, end)))
    return [ext[i] for i in range(n) if keep[i]]


def _area(points: list[tuple[int, int]]) -> float:
    """Absolute shoelace area in square degrees."""
    total = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True))
    return abs(total) / 2 / (PRECISION * PRECISION)


def _ring_points(raw_ring: object) -> list[tuple[int, int]]:
    """Rounds a source ring, dropping consecutive duplicate points and the closing point(s)."""
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
    return points


def _prepare_ring(raw_ring: object, tolerance: float) -> list[tuple[int, int]] | None:
    points = _ring_points(raw_ring)
    if len(points) < 3:
        return None
    simplified = _douglas_peucker_closed(points, tolerance)
    if len(simplified) < 4:
        # Never reduce a ring below 4 points: keep evenly spaced original vertices instead.
        simplified = [points[(i * len(points)) // 4] for i in range(4)] if len(points) >= 4 else points
    return simplified


def _convert_polygon(raw_polygon: object, tolerance: float, min_area: float) -> tuple[float, list] | None:
    """Returns (outer ring area, [outer, *holes above min_area]), or None for an unusable polygon."""
    if not isinstance(raw_polygon, list) or not raw_polygon:
        return None
    outer = _prepare_ring(raw_polygon[0], tolerance)
    if outer is None:
        return None
    holes = [_prepare_ring(raw_hole, tolerance) for raw_hole in raw_polygon[1:]]
    return _area(outer), [outer, *(hole for hole in holes if hole is not None and _area(hole) >= min_area)]


def _convert_geometry(geometry: object, tolerance: float, min_area: float) -> list[list[list[int]]]:
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return []
    raw_polygons = [geometry.get("coordinates")] if geometry.get("type") == "Polygon" else geometry.get("coordinates")
    if not isinstance(raw_polygons, list):
        raise BasemapError("source geometry coordinates must be a list")
    candidates = [c for c in (_convert_polygon(p, tolerance, min_area) for p in raw_polygons) if c is not None]
    if not candidates:
        return []
    # Never delete a tiny nation: keep its largest polygon even when it is below the area floor.
    kept = [rings for area, rings in candidates if area >= min_area] or [max(candidates, key=lambda item: item[0])[1]]
    return [[[value for point in ring for value in point] for ring in rings] for rings in kept]


def _iso2(properties: dict) -> str | None:
    values = (properties.get(key) for key in ("ISO_A2_EH", "ISO_A2"))
    return next((value for value in values if isinstance(value, str) and ISO2_RE.fullmatch(value)), None)


def _feature_to_country(feature: object, tolerance: float, min_area: float) -> dict | None:
    """Converts one source feature; None for a non-object feature or one without usable polygons."""
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties") or {}
    name = properties.get("NAME") or properties.get("ADMIN")
    if not isinstance(name, str) or not name.strip():
        raise BasemapError("source feature has no NAME or ADMIN property")
    polygons = _convert_geometry(feature.get("geometry"), tolerance * PRECISION, min_area)
    if not polygons:
        return None
    iso2 = _iso2(properties)
    direct = iso2 is not None and properties.get("ISO_A2") == iso2
    return {"iso2": iso2, "name": name.strip()[:MAX_NAME_LENGTH], "polygons": polygons, "_direct": direct}


def _resolve_shared_iso2(countries: list[dict]) -> None:
    """ISO_A2_EH repeats the parent code on some dependencies (e.g. Ashmore and Cartier Islands -> AU).

    The feature whose own ISO_A2 carries the code keeps it; the others are drawn with iso2 null.
    """
    by_code: dict[str, list[dict]] = {}
    for country in countries:
        if country["iso2"] is not None:
            by_code.setdefault(country["iso2"], []).append(country)
    for code, holders in by_code.items():
        if len(holders) < 2:
            continue
        direct = [c for c in holders if c["_direct"]]
        if len(direct) != 1:
            raise BasemapError(f"source has ambiguous iso2 {code!r} ({', '.join(c['name'] for c in holders)})")
        for country in holders:
            if country is not direct[0]:
                country["iso2"] = None
    for country in countries:
        del country["_direct"]


def _header_fields(fmt: str, dataset_id: str, version: str) -> dict:
    return {"format": fmt, "schema_version": 1, "dataset_id": dataset_id, "version": version}


def build_data(source: object, dataset_id: str, version: str, tolerance: float, min_area: float) -> bytes:
    if not isinstance(source, dict) or source.get("type") != "FeatureCollection":
        raise BasemapError("source must be a GeoJSON FeatureCollection")
    features = source.get("features")
    if not isinstance(features, list):
        raise BasemapError("source features must be a list")
    countries = [c for c in (_feature_to_country(f, tolerance, min_area) for f in features) if c is not None]
    _resolve_shared_iso2(countries)
    countries.sort(key=lambda c: (c["name"], c["iso2"] or ""))
    document = _header_fields(DATA_FORMAT, dataset_id, version)
    document |= {"projection": PROJECTION, "precision": PRECISION, "countries": countries}
    validate_data(document)
    raw = (json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise BasemapError(f"built data file is {len(raw)} bytes, above the {MAX_BYTES} limit; raise --tolerance")
    return raw


def build_manifest(raw: bytes, file_name: str, dataset_id: str, version: str, created_at: str, source: dict) -> bytes:
    manifest = _header_fields(MANIFEST_FORMAT, dataset_id, version)
    manifest |= {"file": file_name, "bytes": len(raw), "sha256": sha256_hex(raw), "created_at": created_at, "source": source}
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
    raw = build_data(_strict_json(source_raw, "source GeoJSON"), args.dataset_id, args.version, args.tolerance, args.min_area)
    created_at = args.created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_info = {"name": args.source_name, "url": args.source_url, "license": args.source_license}
    source_info["source_sha256"] = sha256_hex(source_raw)
    manifest = build_manifest(raw, file_name, args.dataset_id, args.version, created_at, source_info)
    out_dir = check_dir_argument(Path(args.out_dir), "--out-dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / file_name, raw)
    atomic_write(out_dir / manifest_name, manifest)
    summary = validate_dir(out_dir, manifest_name)
    print(
        f"OK: built {summary['dataset_id']} {summary['version']} -> {out_dir / file_name} ({summary['bytes']} bytes, "
        f"{summary['countries']} countries, {summary['points']} points, sha256 {summary['sha256']})"
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
    if active.is_file() and (current := active.read_bytes()) != manifest_raw:
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
    print(f"active:   {active}\nprevious: {previous}")
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

    for name, func, help_text in (
        ("rollback", cmd_rollback, "restore manifest.previous.json atomically"),
        ("status", cmd_status, "report the active and previous versions"),
    ):
        target_only = sub.add_parser(name, help=help_text)
        target_only.add_argument("--target", required=True)
        target_only.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BasemapError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
