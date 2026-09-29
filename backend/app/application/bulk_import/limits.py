"""Bounded-processing constants for the bulk-import pipeline, mirroring the shape of
app/application/svg_sanitizer.py's MAX_SVG_FILE_SIZE_BYTES/MAX_RASTER_FILE_SIZE_BYTES:
a single, explicit ceiling checked before any real parsing work is attempted."""

MAX_BULK_IMPORT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_BULK_IMPORT_ROWS = 5000
BULK_IMPORT_COMMIT_BATCH_SIZE = 200

# SEC (Codex PR #50 review, ROUND 2, finding #1): how long a run_commit delivery's claimed
# commit lease (BulkImportJob.commit_lease_id/commit_lease_expires_at) is honored before
# another delivery may reclaim it. Long enough to comfortably cover one batch checkpoint's
# normal processing time (renewed at every checkpoint, so a healthy delivery never lets it
# lapse); short enough that recovery from a crashed/stalled delivery doesn't leave the job
# stuck for long.
BULK_IMPORT_COMMIT_LEASE_SECONDS = 60

# SEC (Codex PR #50 review, ROUND 3, finding #1): with Celery's default (early)
# acknowledgment, a worker process that dies while holding a commit lease is never
# redelivered by the broker -- BULK_IMPORT_COMMIT_LEASE_SECONDS above lets another
# delivery reclaim ownership once the lease expires, but only if some delivery actually
# gets invoked again. requeue_stuck_bulk_import_commits (a Celery-beat task, see
# celery_app.py's beat_schedule) is the bounded sweeper that makes that happen: it polls
# for jobs stuck in "committing" past their lease and re-dispatches the commit task for
# up to this many of them per sweep, so a single crash can never turn into an unbounded
# burst of redispatch work.
BULK_IMPORT_SWEEPER_MAX_JOBS_PER_SWEEP = 50

# SEC (Codex PR #50 review, ROUND 4, blocker 1): BULK_IMPORT_SWEEPER_MAX_JOBS_PER_SWEEP
# above bounds how much work one sweep can do, but says nothing about how many times a
# single, deterministically-failing job gets re-dispatched — without a per-job cap, a job
# whose commit always raises before its first lease renewal would have its lease expire,
# get reclaimed by the very next sweep, fail again, and repeat forever at the sweeper's
# fixed interval. run_commit (service.py) increments BulkImportJob.commit_attempt_count
# atomically as part of every lease claim (original dispatch or sweeper redispatch alike)
# and finalizes the job as committed_with_errors instead of processing once this bound is
# exceeded, so the sweeper's own query (status='committing') stops reselecting it.
BULK_IMPORT_COMMIT_MAX_ATTEMPTS = 5

# SEC (Codex PR #50 review, finding #4): MAX_BULK_IMPORT_FILE_SIZE_BYTES above only
# bounds the *compressed* upload — a small, honestly-compressible .xlsx (a zip) can still
# decompress to a huge amount of data (a "zip bomb"), exhausting worker memory/CPU inside
# openpyxl.load_workbook itself, before MAX_BULK_IMPORT_ROWS ever gets a chance to apply
# (that cap only limits rows we chose to materialize, not what load_workbook does
# internally to open the archive). parsing.py checks these against the zip's own central
# directory (`ZipInfo.file_size`/`.compress_size`) before ever calling load_workbook.
MAX_BULK_IMPORT_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_BULK_IMPORT_ZIP_ENTRIES = 50  # a real .xlsx has on the order of 10-20 entries
# A real .xlsx's XML parts compress well (highly repetitive markup/shared strings) but not
# thousands-to-one — 100:1 is generous headroom above what legitimate XML compression
# achieves in practice, while still catching a member built purely to decompress huge
# (e.g. a run of a single repeated byte, which readily compresses beyond 1000:1).
MAX_BULK_IMPORT_ZIP_COMPRESSION_RATIO = 100
