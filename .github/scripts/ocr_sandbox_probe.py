"""Runs inside the celery-worker container (stdin to `python -`): proves the production image can run
sandboxed OCR. Exit codes are named by compose_smoke.OCR_EXIT_MESSAGES.

20 the OCR engine is missing, 21 the seccomp network filter cannot be installed under this container's
runtime profile, 22 a sandboxed child could still create a socket, 23 the real pipeline did not read a
rendered scanned page, 24 the Landlock filesystem policy cannot be enforced under this container's runtime
profile, 25 a sandboxed child could still read its parent's process environment, 26 a sandboxed child could
still signal or truncate other same-UID files and processes."""
import io
import os
import subprocess
import sys

from app.application.catalog_documents.extraction.sandbox import landlock_available, seccomp_available
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

if not landlock_available():
    print("landlock unavailable")
    sys.exit(24)
reader = subprocess.run(  # noqa: S603
    [sys.executable, "-c",
     "import os\n"
     "from app.application.catalog_documents.extraction.sandbox import install_landlock as i, default_read_paths as d\n"
     "i(read_paths=d())\n"
     "try:\n"
     "    open('/proc/%d/environ' % os.getppid()).read(); print('READ')\n"
     "except OSError:\n"
     "    print('denied')"],
    capture_output=True, text=True, timeout=30, check=False,
)
if reader.returncode != 0 or reader.stdout.strip() != "denied":
    print("parent environment readable: " + reader.stdout.strip()[:40])
    sys.exit(25)

import tempfile  # noqa: E402

target_fd, target_path = tempfile.mkstemp(prefix="ocr-probe-")
os.close(target_fd)
victim = subprocess.Popen(["/bin/sleep", "30"])  # noqa: S603
try:
    probe = subprocess.run(  # noqa: S603
        [sys.executable, "-c",
         "import os, signal, sys\n"
         "from app.application.catalog_documents.extraction.sandbox import install_seccomp_no_network as i\n"
         "i()\n"
         "verdicts = []\n"
         "for call in (lambda: os.kill(int(sys.argv[1]), signal.SIGTERM), lambda: os.truncate(sys.argv[2], 0),\n"
         "             lambda: os.chmod(sys.argv[2], 0o000),\n"
         "             lambda: os.close(os.open(sys.argv[2], os.O_RDONLY | os.O_TRUNC)),\n"
         "             lambda: os.setsid(),\n"
         "             lambda: os.setpriority(os.PRIO_PROCESS, int(sys.argv[1]), 15)):\n"
         "    try:\n"
         "        call(); verdicts.append('ALLOWED')\n"
         "    except PermissionError:\n"
         "        verdicts.append('denied')\n"
         "print(','.join(verdicts))", str(victim.pid), target_path],
        capture_output=True, text=True, timeout=30, check=False,
    )
    alive = victim.poll() is None
finally:
    victim.kill()
    victim.wait()
    os.unlink(target_path)
if probe.stdout.strip() != ",".join(["denied"] * 6) or not alive:
    print("sandboxed child could still act on other processes or files: " + probe.stdout.strip()[:60])
    sys.exit(26)

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
