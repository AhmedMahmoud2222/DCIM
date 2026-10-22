# CI Baseline Runtime and Bottleneck Report

**Repository:** `AhmedMahmoud2222/DCIM`
**Source of measurement:** live GitHub Actions runs (public API), branch `main` / PR #190
**Measured:** 2026-10-22
**Workflow:** `.github/workflows/ci.yml` (pre-optimization)

## 1. Measured baseline run

Run `39751184040` (CI workflow, PR #190 "Update MiMo-V2.6-Flash Free model details",
`push`/`pull_request`, conclusion `success`):

| Metric | Value |
|--------|-------|
| Run started | 2026-10-22T08:53:39Z |
| Run completed | 2026-10-22T09:41:58Z |
| **Total CI turnaround** | **~48 min 19 s (2899 s)** |
| Jobs | 8 (all success) |
| Required checks | `backend`, `frontend` |

## 2. Per-job measured durations

| Job | Started | Completed | Duration | Role |
|-----|---------|-----------|----------|------|
| `edge-interop` | 08:53:42 | 08:53:58 | 16 s | SNMPv3 interop |
| `edge-collector` | 08:53:43 | 08:54:08 | 25 s | Edge Collector suite |
| `frontend` | 08:53:43 | 08:55:00 | 1 min 17 s | **required check** |
| `browser-e2e` | 08:53:43 | 08:56:43 | 3 min | Playwright E2E |
| `backend` | 08:53:43 | 09:35:45 | 42 min 2 s | **required check** |
| `backend suite (Python 3.13)` | 08:54:01 | 09:40:25 | 46 min 24 s | Python matrix |
| `backend suite (Python 3.14)` | 08:53:43 | 09:41:02 | 47 min 19 s | Python matrix |
| `backend suite (Python 3.12)` | 08:53:59 | 09:41:58 | 47 min 59 s | Python matrix |

The run's total (48m19s) is gated by the slowest matrix job (Python 3.12, 47m59s).
The required `backend` check finishes at 42m02s; `frontend` at 1m17s.

## 3. Bottleneck analysis

| Bottleneck | Share of 48m run | Notes |
|------------|------------------|-------|
| Python compatibility matrix (3.12/3.13/3.14) | ~46–48 min each, parallel, gates the full run | Runs on **every** PR today; the dominant cost. Each matrix job repeats OCR install, clamd, Postgres provisioning, migrations, mypy and the full ~39-minute regression suite per Python version. |
| Required `backend` check | ~42 min | Full suite incl. OCR engine install (~38 s), clamd (~30 s), DB provisioning, extensions, migrations, mypy, retention hostile validation, extraction apply, and regression tests. |
| `browser-e2e` (Playwright E2E) | ~3 min | Chromium install (~30 s) + backend/Celery/frontend startup + Playwright suite (~79 s). |
| `edge-collector` / `edge-interop` | ~25 s / ~16 s | Small; not the bottleneck. |
| `frontend` | ~1 min 17 s | Already fast; not a bottleneck. |

Key observation: **the Python matrix jobs and the `backend` regression suite are the
bottlenecks**, and both run on every PR today regardless of risk. The matrix repeats
expensive per-version setup (OCR, clamd, Postgres, mypy, full suite) three times in
parallel.

## 4. Step-level breakdown of the `backend` required check (illustrative)

Slowest steps within the `backend` job:

| Step | Duration |
|------|----------|
| Run regression tests | ~39 min (08:56:26 → 09:35:41) |
| Install the OCR engine | ~38 s (08:54:23 → 08:55:01) |
| Install clamd | ~30 s (08:55:01 → 08:55:31) |
| Install dependencies | ~21 s (08:54:02 → 08:54:23) |
| Initialize containers | ~16 s |
| Type check (mypy) | ~11 s |
| Validate extraction apply | ~28 s |

The regression-tests step alone (~39 min) is the single largest contributor to the
required `backend` check.

## 5. A second, fast run for contrast

Run `39756946440` (same branch, re-run) completed in **9 s** — it was a trivial/
cancelled re-run and is not representative of a full validation; it illustrates how
concurrency cancellation short-circuits stale runs, which the new `concurrency`
setting preserves.

## 6. Baseline conclusion

A typical full CI run today costs **~48 minutes**, almost entirely due to the Python
compatibility matrix and the full `backend` regression suite executing on every PR.
Risk-based routing (Fast CI for every PR, Component CI for affected components,
Full CI only for high-risk changes) removes the matrix and E2E/deployment-tooling suite
from low-risk PRs and gives the required `backend` check a fast path for low-risk changes,
targeting a 50–70% reduction in average PR CI turnaround (subject to measurement after
deployment to GitHub Actions).
