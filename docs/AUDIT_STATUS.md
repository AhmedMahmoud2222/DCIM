# Phase 10 audit and validation status

**Prepared:** 2026-09-26. **Source baseline:** PR #29 at `c2d60b34f0ccc546c809a1d3ff15c11befb79a26`, based on `main` `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`. This file is a dated **post-audit status addendum**. It does not alter the historical report by Jules, `PHASE10_INDEPENDENT_AUDIT_REPORT.md`, which describes tests/findings from its original audit of the Phase 10 source.

## Which evidence proves what

| Artifact | Verified scope | Not proved |
|---|---|---|
| Merged Phase 10A/B/C PRs | Those feature implementations are in the pre-PR-29 `main` source | Production deployment or independent proof of every safety property |
| Jules [PR #28](https://github.com/AhmedMahmoud2222/DCIM/pull/28) | Dated baseline audit and its original 25-test Edge Collector observation | The later structural BER rewrite, corrected telemetry ordering or newly added CI jobs |
| Codex [PR #27](https://github.com/AhmedMahmoud2222/DCIM/pull/27) | Initial Vitest CI gate, runtime/doc status correction | Browser CI and Python 3.14 validation at that earlier HEAD |
| Claude [PR #29](https://github.com/AhmedMahmoud2222/DCIM/pull/29) | Reconciled implementations and additional CI/tests; documented source-level fixes and validation | Independent GitHub review approval, until a submitted review exists |
| [GitHub Actions run #80](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36141826654) | Seven successful jobs on PR #29's exact reviewed HEAD, including blocking Python 3.14, browser E2E, Edge and frontend | Production installation, hosted failure-artifact execution or an external penetration test |

At the documentation branch's creation, the original audit report had **not** been updated to reflect PR #29. Its F1/F2 resolved statuses refer to its own historical work, not every subsequently discovered edge case. Its original blanket conclusion about the telemetry pillar must not be read as an adversarial proof of out-of-order sample safety.

## Findings and disposition across baselines

| Finding | At original Phase 10 baseline `d96676c` | On PR #29's reviewed source | Residual / decision |
|---|---|---|---|
| F1 — frontend CI coverage | Vitest and Playwright absent from committed CI | One Vitest step and independent, service-backed Playwright job; both passed on a hosted runner | Actual hosted *failure* artifact upload not yet exercised; controlled local failure produced expected files |
| F2 — SNMP test request-ID flake | Fixed byte offset made the test probabilistically flaky | Structural BER decoder in the test responder; additional variable-ID/community/length/malformed coverage; 55-test suite passed | Historical PR #28 byte-search heuristic was superseded; the **production** collector also now checks outer-datagram exhaustion |
| New — stale latest-status overwrite | Atomic upsert was unconditional and could replace a newer sample with an older one | Conflict key `binding_id`; PostgreSQL guard `stored.sampled_at < incoming.sampled_at`; equal timestamps retain first writer; stale/duplicate ingests return retained row after SELECT/ORM refresh | Continue regression testing with independent database sessions; choose a consistent policy if upstream devices send competing values with identical timestamps |
| F3 — batch transaction model | Handled per-record `NOT_ASSIGNED` and `UNKNOWN_METRIC_MAPPING` rejections yield individual ACKs, accepted rows commit after successful loop; unexpected exceptions roll back the transaction | Intentionally unchanged in PR #29 | Record product-owner approval that unexpected failure means **whole-batch retry**. A missing savepoint is not automatically a correctness defect unless the API contract requires partial success even after unexpected failure |
| F4 — deprecation warnings | Starlette/SQLAlchemy warnings reported by original audit | Not a blocker in passing CI | Track upstream/library migration and remove deprecated API usage in a focused change |
| F5 — supported Python matrix | Earlier CI pinned 3.11; no full 3.14 evidence | 3.11 primary and **blocking** 3.12/3.13/final 3.14 migrated suites passed in CI | The local 3.14.0rc2 typing mismatch was a prerelease-only observation, not a final 3.14 support conclusion |
| Edge Collector CI | Suite not gated originally | Dedicated collector job, plus collector suite in 3.12–3.14 matrix | Maintain separate packaging and explicit repository-root import path |

## Important SQL / contract distinction

The actual Phase 10C code is in `backend/app/application/telemetry_service.py::record_latest_status`. It uses:

```sql
ON CONFLICT (binding_id) DO UPDATE SET ...
WHERE telemetry_latest_status.sampled_at < EXCLUDED.sampled_at
```

This is **not** `ON CONFLICT (managed_asset_id, canonical_metric)`, and the comparison is **not** `>=`. A review describing those different fields and semantics is reviewing a different implementation. `GET /telemetry/latest` orders readings by `occurred_at DESC` and limits the result; it has no window function and does not prove the Phase 10C cache's ordering behavior.

Equal timestamps retain the first successful writer. If competing writers supply different payloads at the same timestamp, the winner depends on which write succeeds first; this is not order-independent arbitration. Re-delivery after that winner is stored is a no-op. No reproducible remaining telemetry race was established in this review.

F3 applies to `POST /api/v1/collectors/{collector_id}/telemetry`, through `ingest_collector_telemetry()`. The function commits before returning its ACK body. `get_db()` rolls back unexpected exceptions, so the caller receives no successful partial ACK after such a failure. The separate discovery `/collectors/{collector_id}/ingest` handler already uses savepoints and is the destination of the packaged Edge client. Whole-transaction failure is the current telemetry implementation, deliberately preserved by PR #29; repository evidence does not establish a product-owner decision requiring or approving partial persistence after unexpected telemetry errors. This remains a contract decision, not a demonstrated data-loss defect or a reason to add savepoints automatically.

The final integration review also reproduced a pre-existing Compose settings failure (missing encryption-key forwarding); see [OPERATIONS.md](OPERATIONS.md). Passing service-backed CI does not exercise that Compose configuration.

The PR #29 integration report documents nine added ordering tests, including three independent-session concurrency cases and one API-contract regression. Six are reported to fail against the old implementation. Source and CI have been inspected; individual local reproductions are separately documented by the implementation author.

## Review and merge requirements

- Recheck the live PR HEAD and base, not an older local `origin/main` snapshot.
- Inspect the actual `binding_id` upsert and SQL conflict guard, the stale-sample fallback SELECT and ORM refresh, and the source-defined equal-timestamp rule.
- Verify `edge_collector/snmp.py` checks `datagram.exhausted`, and verify the structural BER tests independently of that production parser.
- Verify the current seven CI job conclusions and, when reviewing version support, the actual resolved Python patch version and committed blocking matrix configuration.
- Distinguish handled record rejection from unexpected transaction failure. Do not introduce savepoints just to satisfy an earlier audit suggestion without a demonstrated protocol requirement.
- Record an independent review with exact commit SHA and concrete findings in GitHub when that gate is required. External chat statements are not equivalent to a submitted GitHub PR review.
- Do not treat a green PR branch or a successful local audit as authority to merge without the repository owner's decision.

Historical source: [original audit report](../PHASE10_INDEPENDENT_AUDIT_REPORT.md). Subsequent changes and CI: [integration report](../PHASE10_INTEGRATION_REPORT.md). Live acceptance gate: [PR #29](https://github.com/AhmedMahmoud2222/DCIM/pull/29).
