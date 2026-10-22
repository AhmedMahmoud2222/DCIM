# Before / After CI Benchmark

**Repository:** `AhmedMahmoud2222/DCIM`
**Branch:** `ci/risk-based-ci-optimization` (Draft PR; not merged, not deployed)
**Baseline measured:** 2026-10-22 from live GitHub Actions runs (PR #190)
**After:** projected from the implemented workflow; **must be re-measured on GitHub
Actions after the Draft PR is exercised** (this branch is not merged, so no live
"after" run exists yet).

## 1. Baseline ("before") — measured

Run `39751184040` (CI workflow, all 8 jobs success):

| Metric | Value |
|--------|-------|
| Total CI turnaround | **~48 min 19 s (2899 s)** |
| Required `backend` check | ~42 min 2 s |
| Required `frontend` check | ~1 min 17 s |
| Python matrix (3.12/3.13/3.14), each | ~46–48 min (parallel; gates the run) |
| `browser-e2e` (Playwright) | ~3 min |
| `edge-collector` | ~25 s |
| `edge-interop` | ~16 s |

Bottlenecks: the Python compatibility matrix and the `backend` regression suite
(~39 min of the backend job) ran on **every** PR regardless of risk.

A second run (`39756946440`) completed in 9 s — a cancelled/re-run short-circuited by
concurrency, not representative of full validation.

## 2. Projected ("after") — from the implemented workflow

The implemented workflow routes every PR through Fast CI, then runs Component CI / Full CI
only when warranted. The required `backend` check runs a fast path for low-risk changes
and the full preserved suite for high-risk changes; `frontend` always runs its full (~2 min)
suite.

| PR risk class | Jobs run (approx.) | Projected turnaround | vs baseline |
|---------------|--------------------|----------------------|-------------|
| Low-risk (README/docs only) | Fast CI + `backend` fast + `frontend` | **under ~10 min** | **~80% reduction** |
| Low-risk (frontend source only) | Fast CI + component frontend + `backend` fast + `frontend` | **under ~12 min** | **~75% reduction** |
| Component (backend API / edge / DB contract / OpenCode) | Fast CI + one component job + `backend` fast + `frontend` | **under ~15 min** | **~70% reduction** |
| High-risk (migration / auth / security / infra / workflow / shared dep) | Fast CI + `backend` full + `frontend` + Full CI (matrix + E2E + Compose + deployment tooling) | ~baseline (~48 min) | ~0% (full validation required by design) |

**Target:** 50–70% reduction in *average* PR CI turnaround. Since the majority of PRs
are low-risk/component changes, the average is expected to fall well within that range;
high-risk PRs intentionally retain full validation (no compromise on security, coverage,
deployment integrity or branch protection).

## 3. How to measure "after" on GitHub Actions

The Draft PR is not merged and not deployed, so there is no live "after" run yet. To
obtain the real measurement:

1. Open/refresh the Draft PR from branch `ci/risk-based-ci-optimization`.
2. Trigger a CI run (push a commit or open the PR).
3. Record the CI run's total turnaround and per-job durations from the GitHub Actions UI
   or the `/actions/runs/{run_id}/jobs` API.
4. Compare against the baseline above (baseline run `39751184040`, ~48m19s).
5. Update this document with the measured "after" numbers before review sign-off.

## 4. Measurement notes

* Baseline figures are real, measured from the GitHub Actions API (run
  `39751184040`, jobs API). They are not estimates.
* "After" figures are projections from the workflow structure; the fast-path durations
  depend on runner speed and cache hits and must be confirmed by a live run.
* Concurrency cancellation (`cancel-in-progress`) is preserved, so re-runs on new commits
  cancel stale runs — this improves effective turnaround but is not counted in the
  single-run baseline.
