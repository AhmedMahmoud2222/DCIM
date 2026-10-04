"""The extraction pipeline against real PDFs: native text, tables, scanned pages, the OCR fallback
decision, OCR timeout / crash / network attempt, and hostile documents. OCR behaviour that needs no
real engine uses a fake `tesseract` executable; tests that need the real engine skip without it."""

import json
import os
import shutil
import socket
import stat
import sys
import threading
import time

import pytest

from app.application.catalog_documents.extraction import ocr_worker, pipeline
from app.application.catalog_documents.extraction.pipeline import ClaimLost, ExtractionFailure, needs_ocr, run_pipeline
from app.application.catalog_documents.extraction.sandbox import SandboxUnavailable, run_sandboxed_child
from app.core.config import get_settings
from tests._extraction_pdfs import (
    content_stream_bomb_pdf,
    declared_huge_image_pdf,
    deeply_nested_pdf,
    mixed_pdf,
    native_pdf,
    scanned_pdf,
    table_pdf,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox")

REAL_TESSERACT = shutil.which("tesseract")
REQUIRE_REAL_OCR = os.environ.get("REQUIRE_REAL_OCR") == "1"  # CI sets it: a missing engine fails instead of skipping
needs_real_tesseract = pytest.mark.skipif(REAL_TESSERACT is None and not REQUIRE_REAL_OCR, reason="tesseract is not installed")


def test_the_real_ocr_engine_is_present_where_it_is_required():
    if REQUIRE_REAL_OCR:
        assert REAL_TESSERACT is not None, "REQUIRE_REAL_OCR=1 but tesseract is not installed"


DATASHEET = ["CX-100 Technical Specifications", "Typical power: 350 W", "Weight: 12.5 kg", "Heat dissipation: 1195 BTU/hr"]


def settings(**changes):
    return get_settings().model_copy(update=changes)


def fake_tesseract(tmp_path, body: str) -> str:
    """A stand-in OCR engine: a script that prints tesseract-style TSV for `body` lines."""
    path = tmp_path / "tesseract"
    path.write_text(f"#!/usr/bin/python3\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


TSV_HEADER = "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext"
TSV_SCRIPT = f"""
import sys
lines = ["CX-100 Technical Specifications", "Typical power: 350 W", "Weight: 12.5 kg"]
print("{TSV_HEADER}")
for n, line in enumerate(lines, 1):
    for w, word in enumerate(line.split(), 1):
        print(f"5\\t1\\t1\\t1\\t{{n}}\\t{{w}}\\t{{w * 60}}\\t{{n * 40}}\\t50\\t20\\t91\\t{{word}}")
"""


def candidates_by_field(result):
    return (
        {c["field_key"]: c for c in result["candidates"]}
        if isinstance(result, dict)
        else {c["field_key"]: c for c in result.candidates}
    )


# ------------------------------------------------------------------------------ native


def test_native_datasheet_produces_candidates_without_touching_ocr(tmp_path):
    marker = tmp_path / "ocr-was-run"
    engine = fake_tesseract(tmp_path, f"open({str(marker)!r}, 'w').write('x')")
    result = run_pipeline(native_pdf([DATASHEET]), target_names=["CX-100"], settings=settings(catalog_ocr_tesseract_path=engine))
    assert result.method_summary == "native" and result.outcome == "complete" and result.pages_ocr == 0
    assert result.model_resolution == "single_model_matched"
    by_field = candidates_by_field(result)
    assert by_field["power_typical_w"]["value_numeric"] == 350.0
    assert by_field["heat_dissipation"]["unit"] == "BTU/hr"
    assert not marker.exists(), "OCR must not run when native text is sufficient"


def test_multi_page_evidence_records_the_page_each_value_came_from():
    pages = [
        ["CX-100 Technical Specifications", "Typical power: 350 W"],
        ["Unrelated marketing page", "Fast. Reliable."],
        ["Mechanical", "Weight: 12.5 kg", "Dimensions (H x W x D): 44 x 440 x 600 mm"],
    ]
    result = run_pipeline(native_pdf(pages), target_names=["CX-100"], settings=settings())
    where = {c["field_key"]: c["page_number"] for c in result.candidates}
    assert where["power_typical_w"] == 1 and where["weight"] == 3 and where["height"] == 3
    assert result.pages_total == 3 and result.pages_native == 3


def test_multi_model_table_pdf_only_marks_the_targets_column_as_target():
    rows = [
        ["Specification", "CX-100", "CX-200", "CX-300"],
        ["Typical power", "350 W", "520 W", "700 W"],
        ["Weight", "12 kg", "15 kg", "20 kg"],
        ["Dimensions (H x W x D)", "44 x 440 x 600 mm", "88 x 440 x 700 mm", "132 x 440 x 800 mm"],
    ]
    result = run_pipeline(table_pdf([rows]), target_names=["CX-200"], settings=settings())
    assert result.model_resolution == "multi_model_matched"
    target = {c["field_key"]: c["value_numeric"] for c in result.candidates if c["model_match"] == "target"}
    assert target == {"power_typical_w": 520.0, "weight": 15.0, "height": 88.0, "width": 440.0, "depth": 700.0}
    assert all(c["method"] == "table" for c in result.candidates)


def test_conflicting_pages_stay_unresolved():
    pages = [["CX-100 Technical Specifications", "Typical power: 350 W"], ["Typical power: 410 W"]]
    result = run_pipeline(native_pdf(pages), target_names=["CX-100"], settings=settings())
    power = [c for c in result.candidates if c["field_key"] == "power_typical_w"]
    assert len(power) == 2 and all("conflict" in c["flags"] for c in power)


# ------------------------------------------------------------------------------ OCR decision


@pytest.mark.parametrize(
    ("alnum", "images", "expected"),
    [(0, 1, True), (24, 1, True), (25, 1, False), (500, 3, False), (0, 0, False)],
)
def test_ocr_runs_only_for_pages_with_almost_no_text_that_have_an_image(alnum, images, expected):
    assert needs_ocr({"alnum": alnum, "images": images}, settings(catalog_ocr_min_native_chars=25)) is expected


@needs_real_tesseract
def test_scanned_datasheet_goes_through_the_real_ocr_engine():
    result = run_pipeline(scanned_pdf([DATASHEET]), target_names=["CX-100"], settings=settings())
    assert result.method_summary == "ocr" and result.pages_ocr == 1 and result.pages_native == 0
    by_field = candidates_by_field(result)
    assert by_field["power_typical_w"]["value_numeric"] == 350.0 and by_field["power_typical_w"]["method"] == "ocr"
    assert by_field["weight"]["unit"] == "kg"
    assert all(c["confidence"] <= 0.75 for c in result.candidates), "OCR confidence is capped below native"


@needs_real_tesseract
def test_mixed_document_reads_native_pages_natively_and_scanned_pages_by_ocr():
    pdf = mixed_pdf(["CX-100 Technical Specifications", "Typical power: 350 W"], ["Weight: 12.5 kg", "Rated power: 800 W"])
    result = run_pipeline(pdf, target_names=["CX-100"], settings=settings())
    assert result.method_summary == "mixed" and (result.pages_native, result.pages_ocr) == (1, 1)
    methods = {c["field_key"]: (c["method"], c["page_number"]) for c in result.candidates}
    assert methods["power_typical_w"] == ("native", 1) and methods["weight"] == ("ocr", 2)


def test_ocr_with_a_fake_engine_is_deterministic(tmp_path):
    engine = fake_tesseract(tmp_path, TSV_SCRIPT)
    result = run_pipeline(scanned_pdf([DATASHEET]), target_names=["CX-100"], settings=settings(catalog_ocr_tesseract_path=engine))
    assert result.method_summary == "ocr"
    assert {c["field_key"]: c["value_numeric"] for c in result.candidates} == {"power_typical_w": 350.0, "weight": 12.5}
    assert all(c["method"] == "ocr" and "ocr_low_confidence" not in c["flags"] for c in result.candidates)


def test_ocr_page_limit_leaves_the_rest_unread_and_says_so(tmp_path):
    engine = fake_tesseract(tmp_path, TSV_SCRIPT)
    result = run_pipeline(
        scanned_pdf([DATASHEET, DATASHEET, DATASHEET]),
        target_names=["CX-100"],
        settings=settings(catalog_ocr_tesseract_path=engine, catalog_ocr_max_pages=1),
    )
    assert result.pages_ocr == 1 and result.pages_ocr_failed == 2 and result.outcome == "partial"
    assert [w["page"] for w in result.warnings if w["code"] == "ocr_page_limit"] == [2, 3]


# ------------------------------------------------------------------------------ OCR failure modes


def test_ocr_timeout_on_a_scanned_only_document_fails_with_a_fixed_code(tmp_path):
    engine = fake_tesseract(tmp_path, "import time\ntime.sleep(120)")
    started = time.monotonic()
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(
            scanned_pdf([DATASHEET]),
            target_names=["CX-100"],
            settings=settings(catalog_ocr_tesseract_path=engine, catalog_ocr_page_timeout_seconds=2),
        )
    assert raised.value.code == "ocr_timeout" and time.monotonic() - started < 40


def test_ocr_timeout_on_a_mixed_document_keeps_the_native_result_and_marks_it_partial(tmp_path):
    engine = fake_tesseract(tmp_path, "import time\ntime.sleep(120)")
    result = run_pipeline(
        mixed_pdf(["CX-100 Technical Specifications", "Typical power: 350 W"], DATASHEET),
        target_names=["CX-100"],
        settings=settings(catalog_ocr_tesseract_path=engine, catalog_ocr_page_timeout_seconds=2),
    )
    assert result.outcome == "partial" and result.pages_ocr_failed == 1
    assert [w["code"] for w in result.warnings] == ["ocr_timeout"]
    assert candidates_by_field(result)["power_typical_w"]["value_numeric"] == 350.0


def test_ocr_engine_crash_is_reported_as_a_failure_not_an_exception(tmp_path):
    engine = fake_tesseract(
        tmp_path, "import ctypes\nctypes.string_at(0)  # a real segmentation fault (kill is denied in the sandbox)"
    )
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(scanned_pdf([DATASHEET]), target_names=["CX-100"], settings=settings(catalog_ocr_tesseract_path=engine))
    assert raised.value.code == "ocr_failed"


def test_ocr_engine_cannot_open_a_network_connection(tmp_path):
    accepted: list[int] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            accepted.append(1)
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    body = f"""
import socket
try:
    socket.create_connection(("127.0.0.1", {port}), timeout=2).close()
    verdict = "99"
except OSError:
    verdict = "12"
print("{TSV_HEADER}")
for w, word in enumerate(["CX-100", "Technical", "Specifications"], 1):
    print(f"5\\t1\\t1\\t1\\t1\\t{{w}}\\t{{w * 60}}\\t40\\t50\\t20\\t90\\t{{word}}")
for w, word in enumerate(["Weight:", verdict, "kg"], 1):
    print(f"5\\t1\\t1\\t1\\t2\\t{{w}}\\t{{w * 60}}\\t80\\t50\\t20\\t90\\t{{word}}")
"""
    try:
        result = run_pipeline(
            scanned_pdf([DATASHEET]),
            target_names=["CX-100"],
            settings=settings(catalog_ocr_tesseract_path=fake_tesseract(tmp_path, body)),
        )
    finally:
        listener.close()
    assert candidates_by_field(result)["weight"]["value_numeric"] == 12.0, "the engine's connection attempt was not refused"
    assert accepted == [], "something reached the listener"


def test_ocr_fails_closed_when_the_sandbox_cannot_be_installed(monkeypatch, capsys, tmp_path):
    def refuse(**_kwargs):
        raise SandboxUnavailable("no seccomp here")

    monkeypatch.setattr(ocr_worker, "enter_sandbox", refuse)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ocr_worker", "1", "1000", "5", "/bin/false", "eng", "5", "1000", "1000", "5", "1000000", "1", str(tmp_path)],
    )
    ocr_worker.main()
    assert json.loads(capsys.readouterr().out) == {"ok": False, "code": "ocr_sandbox_unavailable"}


def test_ocr_disabled_and_missing_engine_are_reported_not_attempted(tmp_path):
    for changes, code in (
        ({"catalog_ocr_enabled": False}, "ocr_disabled"),
        ({"catalog_ocr_tesseract_path": "/nonexistent/tesseract"}, "ocr_unavailable"),
    ):
        with pytest.raises(ExtractionFailure) as raised:
            run_pipeline(scanned_pdf([DATASHEET]), target_names=["CX-100"], settings=settings(**changes))
        assert raised.value.code == code
    result = run_pipeline(
        mixed_pdf(["CX-100 Technical Specifications", "Weight: 12 kg"], DATASHEET),
        target_names=["CX-100"],
        settings=settings(catalog_ocr_enabled=False),
    )
    assert result.outcome == "partial" and result.warnings[0]["code"] == "ocr_disabled"


def test_a_page_image_declaring_hundreds_of_millions_of_pixels_is_never_decoded(tmp_path):
    marker = tmp_path / "ocr-was-run"
    engine = fake_tesseract(tmp_path, f"open({str(marker)!r}, 'w').write('x')")
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(declared_huge_image_pdf(), target_names=["CX-100"], settings=settings(catalog_ocr_tesseract_path=engine))
    assert raised.value.code == "ocr_image_too_large" and not marker.exists()


# ------------------------------------------------------------------------------ hostile documents


def test_malformed_and_truncated_input_is_rejected_by_the_childs_own_validation():
    for blob in (b"", b"%PDF-1.7\nnot really", os.urandom(4096), native_pdf([DATASHEET])[:-200]):
        with pytest.raises(ExtractionFailure) as raised:
            run_pipeline(blob, target_names=["CX-100"], settings=settings())
        assert raised.value.code in {"document_rejected", "native_crashed", "native_parse_error"}


def test_active_content_in_a_stored_object_is_rejected_again_at_extraction_time():
    from tests.api._document_helpers import pdf_with_javascript

    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(pdf_with_javascript(), target_names=["CX-100"], settings=settings())
    assert raised.value.code == "document_rejected"


def test_a_deeply_nested_structure_is_contained():
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(deeply_nested_pdf(), target_names=["CX-100"], settings=settings())
    assert raised.value.code in {"document_rejected", "native_parse_error", "native_crashed", "native_timeout"}


def test_a_content_stream_decompression_bomb_is_contained():
    """Either the reader's own decompression cap makes the page unreadable (a partial result with a
    warning and nothing extracted) or the sandbox limits stop the child (a fixed failure code). What
    must not happen: a hang, or a worker that grows without bound."""
    started = time.monotonic()
    limits = settings(
        catalog_extraction_native_cpu_seconds=4,
        catalog_extraction_native_wall_seconds=12,
        catalog_extraction_address_space_bytes=512 * 1024 * 1024,
    )
    try:
        result = run_pipeline(content_stream_bomb_pdf(2.0), target_names=["CX-100"], settings=limits)
    except ExtractionFailure as failure:
        assert failure.code in {
            "native_memory",
            "native_crashed",
            "native_timeout",
            "native_parse_error",
            "native_output_too_large",
        }
    else:
        assert result.candidates == [] and result.outcome == "partial"
        assert any(w["code"] in {"page_unreadable", "page_text_truncated"} for w in result.warnings)
    assert time.monotonic() - started < 60


def test_a_huge_text_stream_is_truncated_and_reported():
    lines = [f"Spec line {i} Weight: {i % 7 + 1} kg" for i in range(1, 400)]
    result = run_pipeline(
        native_pdf([lines[:45]] * 8),
        target_names=["CX-100"],
        settings=settings(catalog_extraction_max_page_chars=1000, catalog_extraction_max_total_chars=5000),
    )
    assert result.outcome == "partial"
    assert any(w["code"] == "page_text_truncated" for w in result.warnings)


def test_a_pathological_table_is_bounded(tmp_path):
    header = [["Specification"] + [f"CX-{i}" for i in range(1, 40)]]
    rows = [["Weight", *[f"{n} kg" for n in range(1, 40)]] for _ in range(40)]
    started = time.monotonic()
    result = run_pipeline(table_pdf([header + rows], column_width=34), target_names=["CX-7"], settings=settings())
    assert time.monotonic() - started < 40
    assert len(result.candidates) <= 1000


def test_a_parser_crash_in_a_stage_is_a_fixed_failure_code(monkeypatch):
    real = run_sandboxed_child

    def crash_stage(stage):
        def runner(module, args, stdin_bytes, **kwargs):
            if module.endswith(stage):
                return real("tests.unit._sandbox_child", ["crash"], b"", **kwargs)
            return real(module, args, stdin_bytes, **kwargs)

        return runner

    for stage, code in (("native_worker", "native_crashed"), ("analysis_worker", "analysis_failed")):
        monkeypatch.setattr(pipeline, "run_sandboxed_child", crash_stage(stage))
        with pytest.raises(ExtractionFailure) as raised:
            run_pipeline(native_pdf([DATASHEET]), target_names=["CX-100"], settings=settings())
        assert raised.value.code == code


def test_failure_messages_never_contain_paths_or_exception_text():
    for code, message in pipeline.FAILURE_MESSAGES.items():
        assert "/" not in message and "Traceback" not in message and "Error:" not in message, code
    assert ExtractionFailure("something unexpected").code == "internal_error"


# ------------------------------------------------------------------------------ review findings (PR #98)


def test_a_successful_ocr_run_that_reads_no_words_is_not_a_success(tmp_path):
    """Blank or illegible scans make tesseract exit 0 with no words; that must not look like a read."""
    engine = fake_tesseract(tmp_path, f'print("{TSV_HEADER}")')
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(scanned_pdf([DATASHEET]), target_names=["CX-100"], settings=settings(catalog_ocr_tesseract_path=engine))
    assert raised.value.code == "ocr_no_text"
    mixed = run_pipeline(
        mixed_pdf(["CX-100 Technical Specifications", "Typical power: 350 W"], DATASHEET),
        target_names=["CX-100"],
        settings=settings(catalog_ocr_tesseract_path=engine),
    )
    assert mixed.outcome == "partial" and (mixed.pages_ocr, mixed.pages_ocr_failed) == (0, 1)
    assert [w["code"] for w in mixed.warnings] == ["ocr_no_text"]
    assert candidates_by_field(mixed)["power_typical_w"]["value_numeric"] == 350.0


@needs_real_tesseract
def test_a_blank_scanned_page_is_reported_as_unreadable_with_the_real_engine():
    import io

    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawImage(ImageReader(Image.new("L", (1200, 800), 255)), 20, 300, width=550, height=366)
    pdf.showPage()
    pdf.save()
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(buffer.getvalue(), target_names=["CX-100"], settings=settings())
    assert raised.value.code == "ocr_no_text"


def test_every_child_stage_renews_the_lease_during_the_stage(monkeypatch):
    """Each sandboxed stage is started with the heartbeat as its tick, every third of a lease, so a
    long stage cannot outlive the lease whatever the configured timeouts are."""
    seen: list[tuple[str, object, float]] = []
    real = run_sandboxed_child

    def spy(module, args, stdin_bytes, **kwargs):
        seen.append((module.rsplit(".", 1)[-1], kwargs.get("tick"), kwargs.get("tick_every")))
        return real(module, args, stdin_bytes, **kwargs)

    monkeypatch.setattr(pipeline, "run_sandboxed_child", spy)
    beats: list[int] = []
    heartbeat = lambda: beats.append(1) or True  # noqa: E731
    run_pipeline(
        native_pdf([DATASHEET]),
        target_names=["CX-100"],
        settings=settings(catalog_extraction_lease_seconds=60),
        heartbeat=heartbeat,
    )
    assert [name for name, _, _ in seen] == ["native_worker", "analysis_worker"]
    assert all(tick is heartbeat and every == 20 for _, tick, every in seen)
    assert beats, "the heartbeat also runs between stages"


def test_a_lost_claim_aborts_the_pipeline_without_a_result(tmp_path):
    engine = fake_tesseract(tmp_path, "import time\ntime.sleep(120)")
    started = time.monotonic()
    with pytest.raises(ClaimLost):
        run_pipeline(
            scanned_pdf([DATASHEET]),
            target_names=["CX-100"],
            settings=settings(
                catalog_ocr_tesseract_path=engine, catalog_ocr_page_timeout_seconds=100, catalog_extraction_lease_seconds=3
            ),
            heartbeat=lambda: False,
        )
    assert time.monotonic() - started < 60


def test_the_scratch_directory_is_removed_and_the_engine_runs_inside_the_policy(tmp_path):
    """The fake engine reports what the filesystem policy lets it see; the real engine runs the same way."""
    report = tmp_path / "report.txt"
    engine = fake_tesseract(
        tmp_path,
        f"""
import os
seen = []
for probe in ("/proc/%d/environ" % os.getppid(), "/etc/hostname"):
    try:
        open(probe).read(); seen.append("read:" + probe.rsplit("/", 1)[-1])
    except OSError:
        seen.append("denied:" + probe.rsplit("/", 1)[-1])
try:
    open({str(report)!r}, "w").write("x"); seen.append("wrote-outside")
except OSError:
    seen.append("write-denied")
print("{TSV_HEADER}")
for w, word in enumerate(["Weight:", "12", "kg"], 1):
    print(f"5\\t1\\t1\\t1\\t1\\t{{w}}\\t{{w * 60}}\\t40\\t50\\t20\\t90\\t{{word}}")
print(" ".join(seen))
""",
    )
    result = run_pipeline(
        scanned_pdf([["CX-100 Technical Specifications"]]),
        target_names=["CX-100"],
        settings=settings(catalog_ocr_tesseract_path=engine),
    )
    assert not report.exists(), "the engine wrote outside its scratch directory"
    assert result.pages_ocr == 1


def test_an_ocr_timeout_leaves_no_engine_process_behind(tmp_path):
    """Inside the sandbox the OCR child cannot kill its engine, so on a timeout the supervisor must."""
    import subprocess

    engine = fake_tesseract(tmp_path, "import time\ntime.sleep(300)")
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(
            scanned_pdf([DATASHEET]),
            target_names=["CX-100"],
            settings=settings(catalog_ocr_tesseract_path=engine, catalog_ocr_page_timeout_seconds=2),
        )
    assert raised.value.code == "ocr_timeout"
    time.sleep(0.5)
    listing = subprocess.run(["ps", "-eo", "stat,args"], capture_output=True, text=True).stdout.splitlines()
    assert not [line for line in listing if engine in line and not line.startswith("Z")], "the engine outlived its page"


def test_an_ocr_timeout_removes_its_scratch_directory(tmp_path):
    """The timeout path exits without running the child's cleanup, so it must remove the page image first."""
    import glob
    import tempfile

    pattern = f"{tempfile.gettempdir()}/dcim-ocr-*"
    before = set(glob.glob(pattern))
    engine = fake_tesseract(tmp_path, "import time\ntime.sleep(300)")
    with pytest.raises(ExtractionFailure) as raised:
        run_pipeline(
            scanned_pdf([DATASHEET]),
            target_names=["CX-100"],
            settings=settings(catalog_ocr_tesseract_path=engine, catalog_ocr_page_timeout_seconds=2),
        )
    assert raised.value.code == "ocr_timeout"
    assert set(glob.glob(pattern)) - before == set(), "a timed-out page left its scratch directory behind"
