"""Isolation and failure handling around the importer's sandboxed parser child (Issue #104)."""

import json
import os

import pytest

from app.application.catalog_documents.extraction.sandbox import landlock_available, run_sandboxed_child
from app.application.spatial_import import runner
from app.application.spatial_import.dxf_parser import parse_dxf
from app.application.spatial_import.limits import ParserLimits
from app.application.spatial_import.runner import ParseFailure, parse_in_sandbox
from app.application.spatial_import.sir import SirInvalid, sir_sha256, validate_sir
from tests import spatial_fixtures as fx

pytestmark = pytest.mark.skipif(not landlock_available(), reason="Landlock is required to prove the parser sandbox")


def test_sandboxed_dxf_parse_matches_the_in_process_result_byte_for_byte():
    data = fx.rack_row_dxf(3)
    outcome = parse_in_sandbox(data, "dxf")
    assert sir_sha256(outcome.raw) == sir_sha256(parse_dxf(data).to_dict())
    assert outcome.document.source_format == "dxf" and len(outcome.document.entities) == 7


def test_sandboxed_vsdx_parse_returns_a_validated_sir():
    outcome = parse_in_sandbox(fx.rack_row_vsdx(3), "vsdx")
    assert outcome.document.source_units == "in" and outcome.document.parser_name == "vsdx_zip_xml"
    assert [e.text for e in outcome.document.entities if e.text] == ["RACK-01", "RACK-02", "RACK-03"]


def test_hostile_files_come_back_as_stable_failure_codes():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"../x": b"y"}), "vsdx")
    assert exc.value.code == "vsdx_path_traversal" and "path traversal" in exc.value.reason
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(b"0\nSECTION\n", "dxf")
    assert exc.value.code in {"dxf_truncated", "dxf_malformed"}


def test_unsupported_format_is_refused_by_the_child():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(b"x", "pdf")
    assert exc.value.code == "unsupported_format"


def test_input_larger_than_the_limit_is_refused_by_the_child():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(b"0" * 500, "dxf", limits=ParserLimits(max_input_bytes=100))
    assert exc.value.code == "file_too_large"


def test_timeout_kills_the_parser_and_reports_it():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(fx.rack_row_dxf(2), "dxf", limits=ParserLimits(wall_seconds=0.01))
    assert exc.value.code == "timeout"


def test_parser_crash_under_a_memory_limit_is_a_clean_failure():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(fx.rack_row_dxf(2), "dxf", limits=ParserLimits(address_space_bytes=24 * 1024 * 1024))
    assert exc.value.code == "crash"


def test_oversized_parser_output_is_cut_off():
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(fx.rack_row_dxf(3), "dxf", limits=ParserLimits(max_sir_bytes=200))
    assert exc.value.code == "sir_oversized"


def test_sir_is_revalidated_by_the_parent_even_if_the_child_is_compromised(monkeypatch):
    class _Result:
        returncode, timed_out, output_overflow, aborted = 0, False, False, False

        def __init__(self, payload):
            self.stdout = json.dumps(payload).encode()

    def forged(sir):
        return lambda *a, **k: _Result({"ok": True, "sir": sir})

    base = parse_dxf(fx.rack_row_dxf(1)).to_dict()
    cases = {
        "sir_schema": {**base, "schema_version": 99},
        "sir_too_many_entities": {**base, "entities": base["entities"] * 60_000},
        "sir_coordinate_out_of_range": {**base, "entities": [{"kind": "rect", "ref": "x", "cx": 1e30, "cy": 0, "width": 1, "height": 1}]},
        "sir_text_too_long": {**base, "entities": [{"kind": "text", "ref": "x", "cx": 0, "cy": 0, "text": "A" * 5000}]},
        "sir_bad_entity": {**base, "entities": [{"kind": "exec", "ref": "x"}]},
        "sir_bad_number": {**base, "entities": [{"kind": "rect", "ref": "x", "cx": "1", "cy": 0, "width": 1, "height": 1}]},
        "sir_too_many_points": {**base, "entities": [{"kind": "polygon", "ref": "x", "points": [[0, 0]] * 5000}]},
    }
    for code, payload in cases.items():
        monkeypatch.setattr(runner, "run_sandboxed_child", forged(payload))
        with pytest.raises(ParseFailure) as exc:
            parse_in_sandbox(b"x", "dxf")
        assert exc.value.code == code, (code, exc.value.code)
    monkeypatch.setattr(runner, "run_sandboxed_child", lambda *a, **k: _Result(["not", "a", "dict"]))
    with pytest.raises(ParseFailure) as exc:
        parse_in_sandbox(b"x", "dxf")
    assert exc.value.code == "crash"


def test_validate_sir_rejects_nan_and_booleans():
    base = parse_dxf(fx.rack_row_dxf(1)).to_dict()
    with pytest.raises(SirInvalid):
        validate_sir({**base, "entities": [{"kind": "rect", "ref": "x", "cx": float("nan"), "cy": 0, "width": 1, "height": 1}]})
    with pytest.raises(SirInvalid):
        validate_sir({**base, "entities": [{"kind": "rect", "ref": "x", "cx": True, "cy": 0, "width": 1, "height": 1}]})


def test_child_has_no_credentials_no_network_and_no_filesystem_write_access():
    secret_env = {"DATABASE_URL": "postgresql://leak", "REDIS_URL": "redis://leak", "JWT_SECRET_KEY": "leak"}
    for key, value in secret_env.items():
        os.environ.setdefault(key, value)
    result = run_sandboxed_child("tests.unit._spatial_probe_child", [], b"", wall_seconds=30, stdout_cap_bytes=4096)
    report = json.loads(result.stdout)
    assert report["env_secret_keys"] == []
    assert report["socket"] == "blocked"
    assert report["write_cwd"] == "blocked" and report["write_tmp"] == "blocked"
    assert report["read_parent_environ"] == "blocked"
    assert not os.path.exists("/tmp/spatial_probe_should_not_exist.txt")


def test_no_temp_files_or_processes_are_left_behind_after_failures():
    import glob
    import subprocess

    before = set(glob.glob("/tmp/*"))
    for _ in range(3):
        with pytest.raises(ParseFailure):
            parse_in_sandbox(fx.rack_row_dxf(2), "dxf", limits=ParserLimits(wall_seconds=0.01))
    assert set(glob.glob("/tmp/*")) <= before | {p for p in glob.glob("/tmp/*") if "pytest" in p or "dcim-test" in p}
    import time

    deadline = time.monotonic() + 3
    while True:  # the killed process group is reaped promptly; allow the kernel a moment to drop the entries
        leftovers = subprocess.run(
            ["pgrep", "-f", r"^[^ ]*python[^ ]* -m app\.application\.spatial_import\.worker"], capture_output=True, text=True, check=False
        ).stdout.split()
        if not leftovers or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert leftovers == []
