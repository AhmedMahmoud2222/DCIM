"""Offline basemap tool (scripts/dcim_basemap.py): build determinism, validation, install and rollback."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "dcim_basemap.py"
BUNDLED_DIR = REPO_ROOT / "frontend" / "src" / "features" / "dashboard" / "basemap"
BUNDLED_MANIFEST = "world-50m.v1.manifest.json"


def _load_tool():
    spec = importlib.util.spec_from_file_location("dcim_basemap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


@pytest.fixture(autouse=True)
def _clean_tables():
    """Overrides the suite-wide DB cleanup: this module never touches the database."""
    yield


def _square(lon, lat, size):
    return [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]


FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"NAME": "Mainland", "ISO_A2": "-99", "ISO_A2_EH": "ML"},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    # Outer ring with a dense, nearly straight edge (simplified away) and a hole.
                    [
                        [[0, 0], [5, 0.001], [10, 0], [10, 10], [10, 10], [0, 10], [0, 0]],
                        _square(2, 2, 3),
                    ],
                    # Tiny offshore island below the area floor: dropped, Mainland keeps its main polygon.
                    [_square(20, 20, 0.001)],
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"NAME": "Tiny Nation", "ADMIN": "Tiny", "ISO_A2": "TN", "ISO_A2_EH": "TN"},
            "geometry": {"type": "Polygon", "coordinates": [_square(30, 30, 0.02)]},
        },
        {
            "type": "Feature",
            "properties": {"ADMIN": "Unnamed Code Land", "ISO_A2": "-99", "ISO_A2_EH": "-99"},
            "geometry": {"type": "Polygon", "coordinates": [_square(-40, -40, 2)]},
        },
    ],
}


def _build(tmp_path: Path, name: str = "out", version: str = "1.0.0-r1", fixture=None) -> Path:
    source = tmp_path / f"{name}-source.geojson"
    source.write_text(json.dumps(fixture or FIXTURE), encoding="utf-8")
    out = tmp_path / name
    rc = tool.main(
        ["build", "--geojson", str(source), "--out-dir", str(out), "--version", version, "--created-at", "2026-11-18T00:00:00Z"]
    )
    assert rc == 0
    return out


def _manifest(directory: Path, name: str = "manifest.json") -> dict:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _data_path(directory: Path) -> Path:
    return directory / _manifest(directory)["file"]


def _rewrite_data(directory: Path, mutate) -> None:
    """Applies `mutate` to the parsed data file and re-hashes so only the data rule under test fails."""
    path = _data_path(directory)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    _write_data_bytes(directory, json.dumps(data, separators=(",", ":")).encode())


def _write_data_bytes(directory: Path, raw: bytes) -> None:
    _data_path(directory).write_bytes(raw)
    manifest = _manifest(directory)
    manifest.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite_manifest(directory: Path, **changes) -> None:
    manifest = _manifest(directory)
    manifest.update(changes)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())} if directory.exists() else {}


# --------------------------------------------------------------------------- build


def test_build_is_byte_stable_and_converts_geometry(tmp_path):
    first = _build(tmp_path, "a")
    second = _build(tmp_path, "b")
    raw_a = _data_path(first).read_bytes()
    assert raw_a == _data_path(second).read_bytes()
    assert _manifest(first)["sha256"] == _manifest(second)["sha256"] == hashlib.sha256(raw_a).hexdigest()
    assert raw_a.endswith(b"}\n") and b", " not in raw_a and b": " not in raw_a

    data = json.loads(raw_a)
    assert [c["name"] for c in data["countries"]] == ["Mainland", "Tiny Nation", "Unnamed Code Land"]
    mainland, tiny, unnamed = data["countries"]
    assert mainland["iso2"] == "ML"  # ISO_A2_EH wins over an invalid ISO_A2
    assert unnamed["iso2"] is None
    # Duplicate and near-collinear points removed, closing point not repeated, hole kept, tiny island dropped.
    assert mainland["polygons"] == [[[0, 0, 1000, 0, 1000, 1000, 0, 1000], [200, 200, 500, 200, 500, 500, 200, 500]]]
    # A tiny nation is never deleted even though it is below the area floor.
    assert tiny["iso2"] == "TN"
    assert tiny["polygons"] == [[[3000, 3000, 3002, 3000, 3002, 3002, 3000, 3002]]]
    assert _manifest(first)["source"]["source_sha256"] == hashlib.sha256((tmp_path / "a-source.geojson").read_bytes()).hexdigest()


def test_build_resolves_duplicate_iso2_to_the_feature_that_owns_it(tmp_path):
    fixture = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"NAME": "Parent", "ISO_A2": "PA", "ISO_A2_EH": "PA"},
                "geometry": {"type": "Polygon", "coordinates": [_square(0, 0, 5)]},
            },
            {
                "type": "Feature",
                "properties": {"NAME": "Parent Islands", "ISO_A2": "-99", "ISO_A2_EH": "PA"},
                "geometry": {"type": "Polygon", "coordinates": [_square(10, 10, 1)]},
            },
        ],
    }
    data = json.loads(_data_path(_build(tmp_path, fixture=fixture)).read_bytes())
    assert {c["name"]: c["iso2"] for c in data["countries"]} == {"Parent": "PA", "Parent Islands": None}


# --------------------------------------------------------------------------- validate


def test_validate_accepts_a_good_build(tmp_path, capsys):
    out = _build(tmp_path)
    assert tool.main(["validate", str(out)]) == 0
    assert "OK:" in capsys.readouterr().out


def _set_ring(value):
    def mutate(data):
        data["countries"][0]["polygons"][0][0] = value

    return mutate


def _add_duplicate_iso2(data):
    data["countries"][2]["iso2"] = data["countries"][0]["iso2"]


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        (lambda d: _rewrite_manifest(d, sha256="0" * 64), "checksum mismatch"),
        (lambda d: _rewrite_manifest(d, bytes=_manifest(d)["bytes"] + 1), "size mismatch"),
        (lambda d: _rewrite_manifest(d, schema_version=2), "incompatible manifest schema_version"),
        (lambda d: _rewrite_data(d, lambda x: x.update(schema_version=2)), "incompatible data schema_version"),
        (lambda d: _rewrite_manifest(d, format="other"), "manifest format"),
        (lambda d: _rewrite_data(d, lambda x: x.update(format="geojson")), "data format"),
        (lambda d: _rewrite_manifest(d, file="sub/x.json"), "manifest file"),
        (lambda d: _rewrite_manifest(d, file="..\\x.json"), "manifest file"),
        (lambda d: _rewrite_manifest(d, file="a..json"), "manifest file"),
        (lambda d: _rewrite_data(d, lambda x: x.update(version="9.9.9")), "dataset/version mismatch"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, 18001, 0, 0, 100])), "out of range"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, 100, 9001, 0, 100])), "out of range"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, 100.5, 0, 0, 100])), "not an integer"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, True, 0, 0, 100])), "not an integer"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, 100, 0, 0, 100, 5])), "odd length"),
        (lambda d: _rewrite_data(d, _set_ring([0, 0, 100, 0])), "fewer than 3 points"),
        (lambda d: _rewrite_data(d, _add_duplicate_iso2), "duplicate iso2"),
        (lambda d: _rewrite_data(d, lambda x: x.update(countries=[])), "must not be empty"),
        (lambda d: _rewrite_data(d, lambda x: x["countries"][0].update(iso2="fr")), "iso2"),
        (lambda d: _rewrite_data(d, lambda x: x["countries"][0].update(name="")), "name"),
        (lambda d: _write_data_bytes(d, _data_path(d).read_bytes()[:-40]), "not valid JSON"),
        (lambda d: _write_data_bytes(d, b'{"format":"dcim-basemap","x":NaN}'), "non-finite"),
        (lambda d: (d / "manifest.json").write_bytes(b'{"format":'), "not valid JSON"),
        (lambda d: _data_path(d).unlink(), "missing data file"),
    ],
)
def test_validate_rejects_corruption(tmp_path, capsys, corrupt, reason):
    out = _build(tmp_path)
    corrupt(out)
    assert tool.main(["validate", str(out)]) == 1
    assert reason in capsys.readouterr().err


def test_validate_rejects_too_many_points_and_countries(tmp_path, monkeypatch):
    out = _build(tmp_path)
    monkeypatch.setattr(tool, "MAX_POINTS", 10)
    with pytest.raises(tool.BasemapError, match="too many points"):
        tool.validate_dir(out)
    monkeypatch.setattr(tool, "MAX_POINTS", 250_000)
    monkeypatch.setattr(tool, "MAX_COUNTRIES", 2)
    with pytest.raises(tool.BasemapError, match="too many countries"):
        tool.validate_dir(out)


def test_validate_rejects_points_over_the_real_limit():
    ring = [0, 0, 100, 0, 100, 100] * 20_000  # 60k points per ring
    data = {
        "format": "dcim-basemap",
        "schema_version": 1,
        "dataset_id": "d",
        "version": "v",
        "projection": "equirectangular",
        "precision": 100,
        "countries": [{"iso2": None, "name": f"c{i}", "polygons": [[ring]]} for i in range(5)],
    }
    with pytest.raises(tool.BasemapError, match="too many points"):
        tool.validate_data(data)


# --------------------------------------------------------------------------- install / rollback / status


def test_install_then_rollback_keeps_both_data_files(tmp_path, capsys):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    v2 = _build(tmp_path, "v2", version="2.0.0")
    target = tmp_path / "map-data"

    assert tool.main(["install", str(v1), "--target", str(target)]) == 0
    assert _manifest(target)["version"] == "1.0.0"
    assert not (target / "manifest.previous.json").exists()

    assert tool.main(["install", str(v2), "--target", str(target)]) == 0
    assert _manifest(target)["version"] == "2.0.0"
    assert _manifest(target, "manifest.previous.json")["version"] == "1.0.0"
    assert (target / "basemap-1.0.0.json").is_file() and (target / "basemap-2.0.0.json").is_file()

    assert tool.main(["status", "--target", str(target)]) == 0
    status = capsys.readouterr().out
    assert "active:   natural-earth-admin0-50m 2.0.0" in status and "previous: natural-earth-admin0-50m 1.0.0" in status

    assert tool.main(["rollback", "--target", str(target)]) == 0
    assert _manifest(target)["version"] == "1.0.0"
    assert _manifest(target, "manifest.rolledback.json")["version"] == "2.0.0"
    assert not (target / "manifest.previous.json").exists()
    assert (target / "basemap-1.0.0.json").is_file() and (target / "basemap-2.0.0.json").is_file()
    assert tool.validate_dir(target)["version"] == "1.0.0"
    assert not [p for p in target.iterdir() if p.name.startswith(".tmp-")]


def test_install_failure_leaves_target_untouched(tmp_path):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    bad = _build(tmp_path, "bad", version="2.0.0")
    target = tmp_path / "map-data"
    assert tool.main(["install", str(v1), "--target", str(target)]) == 0
    before = _snapshot(target)

    _rewrite_manifest(bad, sha256="f" * 64)
    assert tool.main(["install", str(bad), "--target", str(target)]) == 1
    assert _snapshot(target) == before

    fresh = tmp_path / "never-created"
    assert tool.main(["install", str(bad), "--target", str(fresh)]) == 1
    assert not fresh.exists()


def test_install_refuses_to_overwrite_a_different_data_file(tmp_path):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    target = tmp_path / "map-data"
    target.mkdir()
    (target / "basemap-1.0.0.json").write_bytes(b"something else")
    before = _snapshot(target)
    assert tool.main(["install", str(v1), "--target", str(target)]) == 1
    assert _snapshot(target) == before


def test_rollback_without_previous_fails_cleanly(tmp_path, capsys):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    target = tmp_path / "map-data"
    assert tool.main(["install", str(v1), "--target", str(target)]) == 0
    before = _snapshot(target)
    assert tool.main(["rollback", "--target", str(target)]) == 1
    assert "no previous manifest" in capsys.readouterr().err
    assert _snapshot(target) == before


def test_rollback_refuses_an_invalid_previous(tmp_path):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    v2 = _build(tmp_path, "v2", version="2.0.0")
    target = tmp_path / "map-data"
    tool.main(["install", str(v1), "--target", str(target)])
    tool.main(["install", str(v2), "--target", str(target)])
    (target / "basemap-1.0.0.json").write_bytes(b"corrupted")
    before = _snapshot(target)
    assert tool.main(["rollback", "--target", str(target)]) == 1
    assert _snapshot(target) == before


def test_status_reports_invalid_active(tmp_path, capsys):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    target = tmp_path / "map-data"
    tool.main(["install", str(v1), "--target", str(target)])
    (target / "basemap-1.0.0.json").write_bytes(b"{}")
    assert tool.main(["status", "--target", str(target)]) == 1
    assert "INVALID" in capsys.readouterr().out
    assert tool.main(["status", "--target", str(tmp_path / "empty")]) == 0


@pytest.mark.parametrize("command", ["install", "rollback", "status"])
def test_path_traversal_in_target_is_refused(tmp_path, capsys, command):
    v1 = _build(tmp_path, "v1", version="1.0.0")
    target = str(tmp_path / "x" / ".." / "map-data")
    argv = [command, str(v1), "--target", target] if command == "install" else [command, "--target", target]
    assert tool.main(argv) == 1
    assert "path traversal" in capsys.readouterr().err
    assert not (tmp_path / "map-data").exists()


@pytest.mark.parametrize("file_name", ["../evil.json", "sub/evil.json", "a..json", "evil.txt", "https:x.json"])
def test_path_traversal_in_build_file_is_refused(tmp_path, capsys, file_name):
    source = tmp_path / "s.geojson"
    source.write_text(json.dumps(FIXTURE), encoding="utf-8")
    argv = ["build", "--geojson", str(source), "--out-dir", str(tmp_path / "o"), "--version", "1", "--file", file_name]
    assert tool.main(argv) == 1
    assert "--file" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


# --------------------------------------------------------------------------- bundled basemap drift


def test_bundled_basemap_validates():
    summary = tool.validate_dir(BUNDLED_DIR, BUNDLED_MANIFEST)
    manifest = _manifest(BUNDLED_DIR, BUNDLED_MANIFEST)
    assert manifest["file"] == "world-50m.v1.json"
    assert summary["dataset_id"] == "natural-earth-admin0-50m" and summary["version"] == "5.1.2-r1"
    assert summary["countries"] >= 170
    assert summary["bytes"] <= 1_200_000
    data = json.loads((BUNDLED_DIR / "world-50m.v1.json").read_bytes())
    codes = {c["iso2"] for c in data["countries"]}
    assert {"AE", "SA", "EG", "SG", "BH", "MT", "FR", "NO", "MV", "MC"} <= codes
