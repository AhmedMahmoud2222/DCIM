# Phase 10 Post-Audit Remediation and Integration Report

**Version:** 1.1
**Date:** 2026-09-25
**Branch:** `claude/intelligent-edison-94mvbo`
**Pull request:** [#29](https://github.com/AhmedMahmoud2222/DCIM/pull/29)
**Status:** AT THE REVIEW GATE. NOT MERGED. Merge requires explicit approval.

---

## 1. Exact SHAs

| Artifact | SHA |
|---|---|
| Baseline (`main`) | `d96676c0534397c7320f09ca3fb4c5c3d72f86ba` |
| PR #27 head (Codex) | `c430f8445f1e82f941a6fb0192df4b7440335b15` |
| PR #28 head (Jules) | `c710309ea437b5273c7be5453fc65599696df8dd` |
| Integration head | `09328a6456e29804baa412cf88c739cdc9f978bf` |

Both PR heads were verified against GitHub before merging. Each matched the head its pull request reported.

### Commit sequence

```
09328a6 ci: put the repository root on PYTHONPATH for the edge-collector job
4d1bbbf ci: gate Python 3.12/3.13 and report 3.14, and align the README runtime claims
0e47173 ci: add Edge Collector and service-backed Playwright jobs (issue #26)
a06f842 test(edge): parse SNMP requests structurally instead of by byte search
ed19695 fix(telemetry): reject out-of-order latest-status samples via a guarded upsert
2642224 ci: retain exactly one frontend Vitest step after reconciling #27 and #28
b5020cc Merge origin/audit/phase10-post-merge-14423236032059353664   <- PR #28
ca24dab merge: reconcile Codex PR #27                                 <- PR #27
```

Both source branches are merged with `--no-ff`. No commit from either branch is duplicated, and both authorships survive in history.

---

## 2. Combined diff against the baseline

```
 .github/workflows/ci.yml                                   | 324 +++++++++++++
 .gitignore                                                 |   3 +
 ARCHITECTURE_REVIEW.md                                     |  19 +-
 PHASE10_INDEPENDENT_AUDIT_REPORT.md                        | 189 ++++++++
 README.md                                                  |  29 +-
 backend/app/application/telemetry_service.py               |  70 ++--
 backend/scripts/create_admin.py                            |  42 +-
 backend/tests/api/test_telemetry_port_status.py            |  57 +++
 backend/tests/integration/test_telemetry_ordering_concurrency.py | 202 ++++++++
 backend/tests/unit/test_telemetry_port_status.py           | 130 ++++++
 edge_collector/scheduler.py                                |   2 +-
 edge_collector/snmp.py                                     |  14 +-
 edge_collector/tests/test_snmp.py                          | 300 ++++++++++--
 13 files changed, 1319 insertions(+), 62 deletions(-)
```

Two files carry production code changes: `telemetry_service.py` and `snmp.py`. Everything else is tests, CI, documentation or a bootstrap script.

---

## 3. Reconciliation of PR #27 and PR #28

Both pull requests independently added `npm test` to the frontend CI job. Merging both produced two Vitest executions in one job.

| Item | Disposition |
|---|---|
| `.github/workflows/ci.yml` Vitest step | PR #27's named step kept. PR #28's unnamed duplicate removed. One Vitest execution remains, verified by `grep -c "npm test"`. |
| `README.md` | Codex's rewrite kept, then extended in commit `4d1bbbf` to state what CI gates rather than what `requires-python` permits. |
| `ARCHITECTURE_REVIEW.md` §51 | Codex's addendum kept unchanged. |
| `PHASE10_INDEPENDENT_AUDIT_REPORT.md` | Jules' report kept unchanged. Section 8 below records what still needs correcting in it. |
| `edge_collector/tests/test_snmp.py` | Jules' fix superseded. Section 5 gives the evidence. |

Neither PR #27 nor PR #28 was merged into `main`. Both remain open.

---

## 4. Task A. Telemetry ordering and concurrency

### The defect

`record_latest_status()` in `backend/app/application/telemetry_service.py` upserted `TelemetryLatestStatus` with an unconditional `ON CONFLICT DO UPDATE`. Whichever ingest committed last won, regardless of when its sample was taken.

Jules' audit examined this function and reported the upsert as atomic, which it was. Atomicity was never the gap. Ordering was.

A poller retrying a delayed batch, or a slow poller losing a race to a faster one, carries an older `sampled_at` and overwrote the newer reading. The rack-elevation overlay then showed a recovered link as `DOWN` until the next poll.

### The fix

The guard sits in the statement:

```sql
ON CONFLICT (binding_id) DO UPDATE SET ...
WHERE telemetry_latest_status.sampled_at < excluded.sampled_at
```

A read-then-write in Python cannot provide this. Two sessions both read the same "before" row, both conclude they are newer, and the slower one overwrites the faster one. PostgreSQL evaluates this `WHERE` after taking the conflicting row's lock and re-reading its committed version, so a concurrent writer sees the other session's committed `sampled_at` and declines, in one statement.

### Defined behaviour

| Case | Outcome |
|---|---|
| `sampled_at` strictly newer | Applied. |
| `sampled_at` strictly older (stale) | Declined. Stored row keeps its status, payload, `sampled_at` and `received_at`. |
| `sampled_at` equal (duplicate) | No-op, first writer wins. No ordering evidence favours either writer, so the outcome stays independent of arrival order. |
| No stored row | Applied. The guard only runs on conflict. |

`received_at` is deliberately left alone on a decline. It records when the winning sample arrived.

### API contract

The signature and return type are unchanged. `record_latest_status()` returns the row that is authoritative after the call: this call's row when applied, the retained newer row when not.

A declined sample is still `200` with the same `PortStatusOut` body. It is not an error. A poller retrying a delayed batch has done nothing wrong and must not be driven into a retry loop by a `409`.

### Test evidence

| Test | Level | Fails before the fix |
|---|---|---|
| `test_stale_sample_does_not_overwrite_a_newer_cached_status` | unit | yes |
| `test_duplicate_sample_at_an_equal_timestamp_is_a_no_op` | unit | yes |
| `test_newer_sample_replaces_the_cached_status` | unit | no (control) |
| `test_first_sample_for_a_binding_is_always_applied` | unit | no (control) |
| `test_ordering_guard_is_scoped_per_binding` | unit | no (control) |
| `test_concurrent_out_of_order_samples_leave_the_newest_stored` | integration | yes |
| `test_a_concurrent_stale_sample_never_wins_against_a_newer_one` | integration | yes |
| `test_concurrent_duplicate_samples_converge_on_one_row` | integration | yes |
| `test_stale_ingest_keeps_the_http_contract_and_returns_the_retained_status` | api | yes |

Six of the nine fail against the previous implementation, verified by reverting `telemetry_service.py` alone and re-running. The three controls pass on both sides, which is what makes them controls.

The three concurrency tests use independent `AsyncSession` instances on their own connections from a dedicated engine, following `tests/integration/test_phase8_concurrency.py`. They are genuine races, not sequential simulations. One dispatches twelve shuffled samples for one binding; one repeats a two-writer race eight times with the dispatch order alternating; one sends ten concurrent re-deliveries of a single sample.

---

## 5. Task B. SNMP test reliability

### Two wrong assumptions

The original harness read the request ID at `request[18:22]`. `_integer()` in `edge_collector/snmp.py` encodes in the shortest form that fits and prepends a `0x00` sign byte when the top bit is set, so the request-ID TLV is 3 to 6 bytes wide and every field after it shifts.

That slice is correct only when the request ID happens to encode to four content bytes. `get()` draws it from `secrets.randbelow(2**31 - 1) + 1`, and 2139095040 of those 2147483647 values encode to four bytes. The baseline test therefore passes 99.609375% of the time and fails about 1 run in 256. Six consecutive baseline runs in this environment all passed, which is what a defect at that rate looks like and why it reached `main`.

PR #28 replaced the slice with `request.find(b"\xa0")`. This fixes the width problem and is correct for every community any test in the repository used. It substitutes a different assumption, though: `0xA0` is the GetRequest tag and also an ordinary byte, so a community string containing it is found first. No existing test exercised that, so the fix is sound for the suite as it stood and fragile for the suite as it grows.

Reproduced directly against `_build_get_request`:

| Community (UTF-8 on the wire) | PR #28 returns | Correct |
|---|---|---|
| `private` | `02047fffffff` | `02047fffffff` |
| `pub\xa0lic` -> `70 75 62 C2 A0 6C 69 63` | 34 bytes of unrelated packet | `02047fffffff` |
| `\xa0secret` | 35 bytes of unrelated packet | `02047fffffff` |

### The replacement

The agent now decodes the request, walking tag/length/value triples from the outer SEQUENCE inward, and echoes the request ID's own encoded TLV at whatever width the collector produced.

The decoder is written out in the test module rather than imported from `snmp.py`. An agent parsing requests with the code under test would agree with the collector about a malformed frame by construction, and the tests would lose the ability to see a BER bug at all.

### Coverage added

6 test functions become 14. The Edge Collector suite goes from 25 tests to 55.

- Request IDs at every encoding width: `0x01`, `0x7F`, `0x80`, `0x1234`, `0x00FFFF`, `0x123456`, `0x7FFFFFFF`, covering both sign-byte cases. Each asserts the observed TLV length, so a regression to a fixed width fails rather than passes by luck.
- Communities carrying `0xA0`, in three positions.
- Long-form lengths at community lengths 126, 127, 128, 255, 256 and 400, crossing both BER boundaries, plus an assertion that a two-byte long-form length is genuinely emitted so the parametrisation cannot silently stop exercising it.
- Ten malformed packet shapes: empty datagram, tag only, header without body, body cut mid-PDU, last byte removed, overstated outer length, wrong outer tag, trailing garbage, a long-form length claiming five bytes, and an indefinite length.
- Wrong request ID, wrong OID, and a non-zero agent error status.

### One collector change

The task instruction was to preserve the collector unless a separate reproducible defect was established. One was.

`_parse_response()` checked four nested BER readers for trailing data and raised `SNMPError("SNMP response has trailing data")`, but never checked the outermost datagram. `read_constructed()` stops at the outer SEQUENCE's declared end and never looks past it, so a well-formed message followed by arbitrary padding was accepted. The function's own error message shows the intent; the outermost level was missed.

The change adds `datagram.exhausted` to that condition. Scope is confirmed narrow: reverting `snmp.py` alone fails exactly one of the 55 tests. The other 54 pass against the collector unmodified.

**This is the only production change outside `telemetry_service.py` and is flagged for the reviewer to accept or reject independently.**

---

## 6. Task C. CI and runtime validation

### New jobs

| Job | Blocking | Purpose |
|---|---|---|
| `edge-collector` | yes | Runs and lints `edge_collector/tests`. Never previously a gate. |
| `browser-e2e` | yes | Playwright on isolated PostgreSQL 16 and Redis 7. |
| `backend suite (Python 3.12)` | yes | Full migrated suite on 3.12. |
| `backend suite (Python 3.13)` | yes | Full migrated suite on 3.13. |
| `backend suite (Python 3.14)` | no | Reports 3.14 status. Section 7 explains why. |

### browser-e2e design

Host ports 5433 and 6380 and database `dcim_e2e`, so isolation from the `backend` job is real rather than incidental.

It is a separate job on purpose. The `backend` job asserts the grants on its own database and exists to prove least-privilege behaviour. Seeding an Administrator with a known password into it would undermine what it validates.

The job provisions the same ordinary non-owner `dcim_app` role, enables the three required extensions, migrates to a single head, runs `bootstrap_privileged_roles.sql`, seeds the Administrator fixture, starts the backend on `127.0.0.1:8000` and Vite on `127.0.0.1:5173`, polls `/api/v1/health/ready` and the frontend with 60 second bounded timeouts, installs Chromium, and runs `npm run test:e2e`.

Readiness polls `/health/ready`, not `/health/live`. Liveness is true before PostgreSQL and Redis are reachable, so waiting on it would hand Playwright a backend that returns 503 on its first request.

Both servers start in their own steps rather than through Playwright's `webServer`, so a startup failure is reported by the step that caused it with its log uploaded. `reuseExistingServer` means Playwright attaches to the running Vite instance.

### Credential handling

`scripts/create_admin.py` gains `--password-from-env VAR` and `--if-exists skip`. The password never reaches argv, which is world-readable through `/proc` and echoed into the CI step trace. Interactive use is unchanged and still prompts twice.

The fixture password lives in the workflow file as a plain value, matching the existing treatment of `JWT_SECRET_KEY` and the database passwords already there. It is a throwaway credential for an ephemeral database. Storing it as a repository secret would mask it in logs while leaving it readable by the job, and would imply a confidentiality that does not apply.

### Failure artifacts, verified

Issue #26 asked for verification by a controlled failing run. A deliberately failing spec was added, run, and removed. It produced, under `frontend/test-results/`:

```
_artifact_probe-controlled-failure-produces-artifacts-chromium/trace.zip
_artifact_probe-controlled-failure-produces-artifacts-chromium/test-failed-1.png
_artifact_probe-controlled-failure-produces-artifacts-chromium/error-context.md
```

These are the exact paths the collection step copies. The collection and upload steps use `if: failure()`, so a job that times out at readiness and never reaches Playwright still uploads both server logs.

---

## 7. Runtime validation results

Local environment: PostgreSQL 16.13, Redis 7, Node 22.22.2, Ubuntu 24.04 x86_64. Each Python version ran against its own database.

| Check | 3.11 | 3.12 | 3.13 | 3.14.0rc2 |
|---|---|---|---|---|
| Dependency install | ok | ok (73 pkgs) | ok (73 pkgs) | ok (73 pkgs) |
| Single Alembic head | `0024_telemetry_impact_map` | same | same | not reached |
| Downgrade to `0010_mvp_alarms` and re-upgrade | pass | pass | pass | not reached |
| Privileged retention bootstrap | pass | pass | pass | not reached |
| `ruff check app tests` | clean | clean | clean | not reached |
| `mypy app` | clean, 110 files | clean | clean | not reached |
| Retention hostile suite | 6 passed | 6 passed | 6 passed | not reached |
| Full migrated backend suite | 693 passed | 693 passed | 693 passed | blocked |
| Edge Collector suite | 55 passed | 55 passed | 55 passed | not reached |

Frontend, Node 22.22.2: Vitest 35 passed across 6 files. `npm run typecheck` clean. `npx eslint . --ext ts,tsx` clean. `npm run build` succeeded. `npm audit --audit-level=high` reported 0 vulnerabilities.

Playwright against the real stack: `phase10b-instantiation.spec.ts` and `phase10c-telemetry-impact.spec.ts` both passed. This meets issue #26's acceptance criterion that both specs are green.

The backend suite count of 693 is the baseline's 690 plus this branch's 9 new tests, less the 6 retention hostile tests reported separately.

### Python 3.14 is not established

Installation succeeds on 3.14.0rc2. Import does not:

```
TypeError: _eval_type() got an unexpected keyword argument 'prefer_fwd_module'
```

raised from `pydantic/_internal/_typing_extra.py` when importing `pydantic.root_model`, with `pydantic==2.13.5` and `pydantic-core==2.46.5`.

This is a release-candidate gap, not a verdict on 3.14. Inspecting the signature on rc2:

```
typing._eval_type(t, globalns, localns, type_params=<sentinel>, *,
                  recursive_guard=frozenset(), format=None, owner=None, parent_fwdref=None)
```

rc2 takes `parent_fwdref`. pydantic 2.13.5 passes `prefer_fwd_module`, which it does not accept. pydantic 2.13.5 targets the final 3.14 typing API, and rc2 predates it.

No final 3.14 build was obtainable in the validation environment. The distribution's package index host is blocked by the environment's outbound proxy, and the available standalone builds stop at `3.14.0rc2`.

Reporting 3.14 as unsupported on this evidence would be wrong. The matrix job settles it on GitHub's runners, where `actions/setup-python` installs a final 3.14.x, without a pre-release blocking every pull request. Promote it to blocking once it reports green; pin the fixed dependency once it reports red.

---

## 8. Findings status

### Resolved on this branch

| ID | Source | Severity | Disposition |
|---|---|---|---|
| F1 | Jules | HIGH | Resolved. One Vitest step in CI, duplicate removed. Playwright now gated by `browser-e2e`, which F1 left open. |
| F2 | Jules | MEDIUM | Resolved differently. The underlying defect is a ~0.39% flake, not a hard failure. Jules' fix removes it but reintroduces fragility for communities carrying `0xA0`; section 5 has both measurements. |
| New | this work | HIGH | Out-of-order telemetry overwrote newer readings. Section 4. |
| New | this work | LOW | `_parse_response()` accepted trailing data after a valid frame. Section 5. |
| New | this work | LOW | Edge Collector suite absent from CI. Resolved by `edge-collector`. |
| New | this work | LOW | `backend/media/` upload storage was not ignored by git. Resolved in `.gitignore`. |

### Open

| ID | Source | Severity | Status |
|---|---|---|---|
| F3 | Jules | LOW | Open. `ingest_collector_telemetry` has no per-record savepoints, so an unexpected exception rolls back preceding valid records in a batch. Outside the assigned scope. Not attempted. |
| F4 | Jules | INFORMATIONAL | Open. Starlette `HTTP_422_UNPROCESSABLE_ENTITY` and SQLAlchemy `.distinct()` deprecation warnings. 32 warnings still reported. |
| F5 | Jules | INFORMATIONAL | Partly closed. A CI matrix now exists. 3.14 remains unestablished. |

### Corrections owed to the independent audit report

`PHASE10_INDEPENDENT_AUDIT_REPORT.md` is unchanged on this branch. Correcting another author's report is theirs to do, and three items need it:

1. F2 is marked "Fix Provided in PR". The fix in that PR does not work for community strings containing `0xA0`. The status should be corrected and the reproduction from section 5 attached.
2. F1 is marked "Fix Provided in PR", but that PR addressed only the Vitest half. Playwright stayed outside CI until this branch. The Edge Collector suite was absent from CI and the report does not record it at all.
3. Section 8 concludes the telemetry pillar is "fundamentally sound, high-quality, and robust" after examining `record_latest_status()`. The function had an ordering defect at the audited SHA. The conclusion should distinguish what was verified from what was inspected without an adversarial concurrency test.

One further item is a matter of precision rather than correctness. F2 is described as "causing SNMP test failure" and the fix as making the suite pass "deterministically". The baseline test is not deterministically broken: it fails about 1 run in 256. Section 5 has the measurement. The distinction matters because it explains why the defect survived review, and because a fix's value is judged differently for a rare flake than for a hard failure.

---

## 9. Acceptance criteria

| Criterion | Status |
|---|---|
| All backend suites run against reconciled code | Met. 693 + 6 on 3.11, 3.12 and 3.13. |
| Frontend suites run | Met. Vitest 35/35, typecheck, lint, build, audit. |
| Edge Collector suite runs | Met. 55 passed on 3.11, 3.12 and 3.13. |
| Playwright suite runs | Met. Both specs pass against a real migrated backend. |
| Single Alembic head | Met. `0024_telemetry_impact_map`. |
| Successful migration upgrade | Met. |
| Supported downgrade validation | Met. Downgrade to `0010_mvp_alarms` and re-upgrade, single head after. |
| Clean lint and typecheck | Met. ruff and mypy clean on backend and `edge_collector`. ESLint and tsc clean. |
| Passing GitHub Actions | See section 10. |
| Existing security and least-privilege validation preserved | Met. The `backend` job is unchanged. The new `browser-e2e` job reproduces its non-owner `dcim_app` pattern. |

---

## 10. GitHub Actions

### First run, head `4d1bbbf42f03acca2f19c27a64a6b4e29ecf1ee2`

https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36139059701

| Job | Result |
|---|---|
| `frontend` | success |
| `browser-e2e` | **success** |
| `edge-collector` | failure |
| `backend`, `backend suite (3.12 / 3.13 / 3.14)` | cancelled when the run failed |

`browser-e2e` passing on a hosted runner settles risk 5 below. The isolated services, role provisioning, migration, privileged bootstrap, Administrator fixture, readiness polling and both Playwright specs all work on GitHub's infrastructure and not only locally.

### `edge-collector` failure and fix

The job failed on all five Edge Collector test modules:

```
ModuleNotFoundError: No module named 'edge_collector'
```

`edge_collector/tests/` has no `__init__.py`, so pytest's rootdir insertion adds that directory to `sys.path` instead of the repository root, and the absolute imports in the test modules cannot resolve.

Local validation missed this. Every local run used `python -m pytest`, which prepends the working directory to `sys.path`; the job invokes the `pytest` console script, which does not. The failure reproduces locally the moment the console script is used, which is how the fix was confirmed rather than guessed.

Fixed in `09328a6` by exporting `PYTHONPATH: ${{ github.workspace }}` on the job, the same mechanism and the same reason as the existing `backend` job. The `backend-runtime-matrix` job already inherited it at job level and was unaffected.

This is a defect in work delivered here, not in the merged branches. It is recorded rather than quietly amended because a CI job that fails on its first real run is exactly the kind of thing a reviewer should see the trace of.

### Second run, head `09328a6456e29804baa412cf88c739cdc9f978bf`

<!-- CI_RESULTS -->

---

## 11. Outstanding risks

1. **Python 3.14 is unresolved.** Section 7. The matrix job reports it; the answer is not in hand yet.
2. **F3 is unfixed.** A batch ingest that raises an unexpected exception still rolls back preceding valid records.
3. **The independent audit report is uncorrected.** Section 8. It currently reads as though F1 and F2 are closed and the telemetry pillar was adversarially verified.
4. **The equal-timestamp rule is a decision, not a deduction.** First-writer-wins at an identical `sampled_at` was chosen because it makes concurrent re-delivery deterministic. A deployment that intends last-writer-wins at equal timestamps would need this changed. It is documented in the function's docstring and covered by tests, so a future change is a visible one.
5. **Closed.** `browser-e2e` passed on a hosted runner in the first CI run. Its failure-artifact path remains verified only locally, since the job has not yet failed on a runner.
6. **The 3.14 job is non-blocking.** If it later fails for a real reason, nothing stops a merge. It needs promoting or pinning once its first result is known.

---

## 12. Proposed merge order

1. **Close PR #27 and PR #28 without merging.** Both are fully contained in PR #29, with authorship preserved through `--no-ff` merges. Merging either first would leave a duplicate Vitest step on `main` until PR #29 lands.
2. **Obtain independent validation of `4d1bbbf42f03acca2f19c27a64a6b4e29ecf1ee2`.** Section 13.
3. **Merge PR #29 into `main`** once CI is green and independent validation reports.
4. **Close issue #26** once `browser-e2e` and the runtime matrix have passed on `main`, recording the 3.14 outcome there.
5. **Open a follow-up for F3** and for promoting or pinning the 3.14 matrix entry.

---

## 13. Requested independent validation

Review is requested of exactly `4d1bbbf42f03acca2f19c27a64a6b4e29ecf1ee2`, covering:

1. **Telemetry ordering under concurrency.** Whether the `ON CONFLICT ... WHERE` guard holds under `READ COMMITTED` for concurrent inserts as well as concurrent updates, whether the equal-timestamp rule is the right call, and whether returning `200` for a declined stale sample is right for every caller.
2. **SNMP BER parsing.** Whether the test-module decoder is itself correct, whether the malformed-packet set has real gaps, and whether the `datagram.exhausted` change to the collector is acceptable or should be reverted.
3. **RBAC and database invariants.** Whether the ordering guard affects any permission boundary or constraint, and whether `browser-e2e`'s role provisioning preserves the least-privilege properties the `backend` job asserts.
4. **CI coverage and test evidence.** Whether the new jobs gate what they claim, whether the Administrator fixture leaks credentials into any log, and whether the failure-artifact path holds on a hosted runner.
5. **Correction of `PHASE10_INDEPENDENT_AUDIT_REPORT.md`,** distinguishing resolved findings from deferred and unverified ones, per section 8.

Every finding should carry severity, file references, reproducible evidence and a disposition.

---

## 14. Gate

This work stops here. PR #29 is open for review and is not merged. Merging into `main` requires explicit approval.
