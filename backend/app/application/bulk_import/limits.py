"""Bounded-processing constants for the bulk-import pipeline, mirroring the shape of
app/application/svg_sanitizer.py's MAX_SVG_FILE_SIZE_BYTES/MAX_RASTER_FILE_SIZE_BYTES:
a single, explicit ceiling checked before any real parsing work is attempted."""

MAX_BULK_IMPORT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_BULK_IMPORT_ROWS = 5000
BULK_IMPORT_COMMIT_BATCH_SIZE = 200
