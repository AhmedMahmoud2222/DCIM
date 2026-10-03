"""Runs inside the celery-worker container (stdin to `python -`): proves the production image can run
sandboxed OCR. Exit codes are named by compose_smoke.OCR_EXIT_MESSAGES.

20 the OCR engine is missing, 21 the seccomp network filter cannot be installed under this container's
runtime profile, 22 a sandboxed child could still create a socket, 23 the real pipeline did not read a
rendered scanned page."""
import io
import os
import subprocess
import sys

from app.application.catalog_documents.extraction.sandbox import seccomp_available
from app.core.config import get_settings

settings = get_settings()
if not (os.path.isabs(settings.catalog_ocr_tesseract_path) and os.access(settings.catalog_ocr_tesseract_path, os.X_OK)):
    print("tesseract missing")
    sys.exit(20)
if not seccomp_available():
    print("seccomp unavailable")
    sys.exit(21)
child = subprocess.run(  # noqa: S603
    [sys.executable, "-c",
     "from app.application.catalog_documents.extraction.sandbox import install_seccomp_no_network as i; i();"
     "import ctypes; print(ctypes.CDLL(None).socket(2, 1, 0))"],
    capture_output=True, text=True, timeout=30, check=False,
)
if child.returncode != 0 or child.stdout.strip() != "-1":
    print("socket not denied: " + child.stdout.strip()[:40])
    sys.exit(22)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.application.catalog_documents.extraction.pipeline import run_pipeline  # noqa: E402

image = Image.new("L", (1600, 400), 255)
draw = ImageDraw.Draw(image)
font = ImageFont.load_default(size=44)
draw.text((40, 40), "CX-100 Technical Specifications", fill=0, font=font)
draw.text((40, 140), "Weight: 12.5 kg", fill=0, font=font)
buffer = io.BytesIO()
image.save(buffer, format="PDF")
try:
    outcome = run_pipeline(buffer.getvalue(), target_names=["CX-100"], settings=settings)
except Exception as exc:  # noqa: BLE001
    print("pipeline failed: " + type(exc).__name__ + " " + str(getattr(exc, "code", "")))
    sys.exit(23)
weights = [c for c in outcome.candidates if c["field_key"] == "weight" and c["value_numeric"] == 12.5]
if outcome.pages_ocr != 1 or not weights:
    print("OCR output unexpected")
    sys.exit(23)
print("tesseract + seccomp network filter + OCR pipeline OK")
