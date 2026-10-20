# Issue #107: AI-assisted operations, M1 architecture

**Document version:** 0.2 (2026-10-20). v0.1 was the 16-line planning stub at `53ce47d`.
**Status:** M1, documentation and contract design only. Nothing here is implemented. No model, provider client, assistant endpoint or write-capable workflow exists in the repository.

## 1. Scope and authority

M1 audits the existing DCIM services and fixes the contracts that M2 (read-only assistant) must meet. It does not authorize code, infrastructure changes, provider accounts, or enabling any write-capable AI workflow. Merge authority stays with the repository owner. Issue #107 stays open.

Safety boundary (unchanged from v0.1): model output never executes an infrastructure write. Autonomous or self-healing execution stays disabled until separately accepted.

## 2. Evidence baselines

| Reference | SHA | Note |
|---|---|---|
| PR #116 base | `703f74e7edcaa515e044095b4e2ce7e4e67917ea` | Merge base with `main` |
| PR #116 HEAD before M1 | `53ce47da054894c467aa0ef0d470bb49f32ba132` | Merge of `main@703f74e` into the branch |
| `main` audited | `92be69284d246868bb1c740bd98d4eba0edd7abc` | G7 readings provenance (#141) |

`main` is 59 commits ahead of the branch. The PR branch tree does not contain the code audited below. Every repository path in the M1 documents was read on `main@92be692`, not in the PR #116 checkout. Backticked paths resolve on `main` and are intentionally not Markdown links, so the PR branch has no broken links. M1 does not merge, rebase or retarget the branch; the integration gap is in section 6.

The issue text names `main@4000a60` as baseline. That commit predates the units registry, correlation, power analytics, cooling and the telemetry G1-G8 work audited here.

## 3. Document set

| Document | Content |
|---|---|
| This file | Scope, baselines, findings, milestone plan, test plan, open decisions |
| [ISSUE_107_M1_DEPENDENCY_MATRIX.md](ISSUE_107_M1_DEPENDENCY_MATRIX.md) | Source audit and per-capability dependency matrix (A) |
| [ISSUE_107_M2_READONLY_ASSISTANT.md](ISSUE_107_M2_READONLY_ASSISTANT.md) | Read-only assistant architecture and M2 contract (B, G) |
| [ISSUE_107_DATA_QUALITY_AND_PREDICTIVE.md](ISSUE_107_DATA_QUALITY_AND_PREDICTIVE.md) | Data quality, anomaly detection, predictive maintenance, capacity (C, D) |
| [ISSUE_107_REMEDIATION_AND_THREAT_MODEL.md](ISSUE_107_REMEDIATION_AND_THREAT_MODEL.md) | Controlled remediation design and threat model (E, F) |

## 4. Headline findings

1. **Scope is enforced only for a short list of permissions.** `SCOPE_AWARE_PERMISSIONS` in `backend/app/application/access_control.py` holds organization/location/rack/equipment/cable/cooling codes. `load_effective_access` moves every other permission a non-global holder has into `inactive_permissions`, so site-restricted users hold no `alarm:*`, `collector:*`, `integration:*`, `power:*`, `capacity:*`, `telemetry:*` or `dashboard:*` permission. This is deny-by-omission, not scoped read.
2. **Alarm, telemetry, collector and power reads carry no site or rack predicate.** `backend/app/api/v1/alarms.py` (`list_alarms`), `telemetry.py` (`latest_readings`, `metric_history`), `collectors.py` and `power_analytics.py` gate on one permission and then query globally. They are safe today only because the permission is unavailable to restricted users. A site-scoped assistant over alarms or telemetry needs scoped-read work first (dependency D1).
3. **Operations API behavior confirmed.** `backend/app/api/v1/operations.py` documents that incidents, collector history and integrations answer 403 identically for existing and missing ids to site-restricted users, and redacts `shared_power_cause` topology (label, rationale, power-scope evidence) without `power:read` (`_hides_topology`, `get_incident`). Tests: `backend/tests/api/test_operations_api.py::test_topology_evidence_is_hidden_from_callers_without_power_read` and `::test_site_restricted_user_is_denied_everywhere_identically_for_real_and_missing_ids`. M2 preserves this.
4. **Effective access is recomputed per request in one MVCC snapshot** (`load_effective_access`, `AuthContext` in `rbac.py`). Explicit group denies win (`effective = allowed - denied`). Per-permission scopes exist (`permission_scopes`, `AuthContext.scope_for`). This is the right primitive for revalidation before async delivery.
5. **Telemetry semantics are strong enough for deterministic checks.** Registry `backend/app/domain/telemetry/registry.py`, v2 semantics `metric_semantics.py` (`V2_SEMANTICS`, `validate_value`), replay ledger (`telemetry_ledger.claim_consumed_keys`), aggregate lineage (`telemetry_lineage.record_lineage`, `evidence_class`) and reading provenance (`ReadingProvenanceStatus` in `telemetry.py`). Cadence is available per integration (`Integration.poll_interval_seconds`), so stale-reading checks need no new schema for cadence.
6. **The existing forecast is a statistical baseline, not a risk model.** `backend/app/application/power_forecast.py` implements `linear_ols_v1` with explicit refusal statuses (`sparse`, `stale`, `missing`, `estimated_mixed`). Its refusal behavior is the template for AI forecasts.
7. **Several dependency issues are closed without recorded independent acceptance.** #99 and #101-#105 show `closed / completed` by the owner on 2026-10-08 and 2026-10-09 with no closing PR attached (except #105 via PR #114). #128 (G1-G10) is open and G9a (PR #140) is open. The independent security review PRs for SEC-ARCH-02 and related items (#171, #158, #161) are still draft. M1 marks these "present in source", not "independently accepted".
8. **Audit redaction is name-based.** `audit_service._redact` masks only a fixed set of key names. Prompts, retrieved text and model output need their own redaction and retention policy before reaching `write_audit_log`.
9. **Nothing exists for LLM provider calls, evidence bundles, claim validation or action plans.** A repository search for provider/assistant code found only incidental matches. Those components are new in M2/M5.

## 5. Failed security check

`github-advanced-security` on HEAD `53ce47d` (run 37259448068, job 111603372647) failed because the Copilot code-scanning review step received `SessionModelError: You have exceeded your monthly quota` (HTTP 402, `errorType: quota`). The log shows no finding in the diff. The cause is an account quota, not repository code. The check is not bypassed or disabled. It needs a re-run after the quota resets or a configuration decision by the owner.

## 6. Branch integration gap and signature policy

- PR #116 reports `mergeable_state: behind`. M1 adds documents only and touches no file changed on `main`. The branch needs a `main` integration before merge; that needs explicit owner authorization (no merge, rebase or retarget in M1).
- Commit `d4b0820` (2026-10-04, author and committer both `AhmedMahmoud2222`, relay email) was unsigned when inspected before M1. HEAD `53ce47d` is a GitHub web-flow merge commit. M1 does not rewrite history or force-push. Open decision: whether the repository requires signed commits on this branch and, if so, whether the owner accepts the historical unsigned commit or authorizes a squash at merge (a merge-time action, not an M1 action).
- The signature state of the M1 commits is recorded in the PR description after publication.

## 7. Milestones

| Id | Deliverable | Depends on | Complexity | Separately testable by |
|---|---|---|---|---|
| M1 | This document set | none | S | Citation review; docs checks |
| M2 | Read-only assistant: typed retrieval, evidence bundle, claim validation, UI | M1, D1-D3 | L | PostgreSQL scope tests, adversarial suite, E2E |
| M3 | Deterministic data-quality checks and operational anomaly detectors | M2 evidence schema, D4 | L | Replay fixtures, detector unit tests, precision/recall report |
| M4 | Correlation reuse and evaluated predictive analytics | M3, data-volume gates | XL | Time-separated evaluation reports |
| M5 | Controlled-action proposal, simulation and approval (design, then implementation) | M2, owner authorization, change-management inputs | XL | Plan hash, approval binding, simulated execution tests |
| M6 | Autonomy-gate tests with execution still disabled | M5 | M | Kill-switch, policy and failure-injection tests |
| M7 | Integration and independent acceptance | M2-M6 | M | Independent red-team and safety review at exact HEAD |

Dependency gates:

- **D1** Scoped read for alarms (and optionally collector health and telemetry) so site-restricted users can be served. Until it ships, M2 answers alarm and telemetry questions only for users who hold the underlying permission globally.
- **D2** Decide the AI permission model (decision 1).
- **D3** Provider data-handling decision (decision 3).
- **D4** Metric-specific expected cadence and exclusions (maintenance windows) for M3. Only `Integration.poll_interval_seconds` exists today.
- M4 predictive maintenance also needs labeled failure and maintenance history that the repository does not hold.

## 8. Unresolved architecture decisions

1. **Permission model.** A new `assistant:use` code that only gates the feature, with per-tool checks of the underlying read permission and scope (recommended), or reuse of existing codes only. In both cases the AI code grants no data access.
2. **Scoped alarm read (D1).** Extend `alarm:read` to scope-aware, or add a read facade. This changes `SCOPE_AWARE_PERMISSIONS` semantics and the 403-for-missing-id contract of the operations API, so the owner decides.
3. **Provider data handling.** Hosted API or self-hosted model, retention, zero-retention terms, region.
4. **Prompt and response retention.** Default retention and who can read stored conversations.
5. **Evidence-only first.** Ship evidence-only (recommended) and enable synthesis behind a flag, or ship both.
6. **Aggregate disclosure.** Counts computed only over the caller's scope (recommended) versus small-group suppression.
7. **Maintenance windows and change calendar.** No change-window model exists in the repository; needed for anomaly exclusions and M5.
8. **SEC-ARCH-02 acceptance.** Whether the M2 release gate requires the independent review PRs (#171 and related) to be accepted first.

## 9. Test plan for M2 onward (proposed)

None of these tests exist. They make M2 acceptance checkable. Tests touching scope run on PostgreSQL because `load_effective_access` uses PostgreSQL-specific SQL.

PostgreSQL / API:

- Per-permission scope: a user holds `equipment:read` for site A and `alarm:read` globally; each tool honors its own code and scope. Groups with different scopes per permission do not combine (extends `backend/tests/api/test_sec_arch_02_permission_scopes.py`).
- Explicit deny: a group deny of `power:read` removes power evidence and power-derived incident fields from bundles and answers.
- Site/rack isolation and moved assets: equipment moved outside the caller's scope between question and delivery is absent from the result.
- Unknown versus unauthorized id: identical response shape and status.
- Aggregate and count leakage: counts, rankings and totals are computed after scope filtering; hidden resources change no visible number.
- Authorization revoked during an async request: revoke after enqueue; delivery is withheld and the stored result is purged.
- Cross-user cache and history: user B never receives user A's bundle, conversation or export, including after A's scope narrows.

Adversarial (a scripted fake provider drives the gate tests; no real model needed):

- Malicious retrieved metadata: asset names, ticket text and datasheet text containing instructions do not change tool selection or raise privilege.
- Unsupported claims and fabricated references: a claim its cited item does not support is rejected; a citation id absent from the bundle is rejected.
- Stale and contradictory evidence: answers state staleness and contradiction and do not choose silently.
- Provider timeout and failure: evidence-only fallback returns the bundle with an explicit status and no retry storm.
- Forbidden tool execution: unlisted tool name, extra arguments, out-of-range window and write verbs are rejected before any query.

Frontend / E2E: read-only states (loading, empty, stale, partial, fallback, denied), source inspection panel, no write control reachable, export contains only the caller's evidence.

Model quality (M3 onward): fixed replay datasets with documented labels; results name the dataset id, detector version and threshold set. Documentation checks establish no accuracy.

## 10. Validation of M1

The PR description lists the checks run on the published HEAD. Documentation checks establish link validity and consistency with the cited source. They establish nothing about runtime behavior or model accuracy.
