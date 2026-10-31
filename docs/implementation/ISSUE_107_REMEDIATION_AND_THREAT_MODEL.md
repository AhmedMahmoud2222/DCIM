# Issue #107 M5/M6: controlled remediation design and threat model

**Document version:** 0.1 (2026-10-20). **DESIGN ONLY. No part of this is implemented, and execution and autonomy stay disabled.** Any write-capable workflow requires explicit owner authorization before code exists. Existing-behavior claims cite `main@92be692`.

## 1. Remediation plan object (PROPOSED)

A plan is typed, immutable and versioned. A change produces a new plan with a new hash and voids earlier approvals.

```json
{
  "plan_id": "uuid",
  "schema_version": 1,
  "action_type": "notification.resend | itsm.ticket_retry | collector.restart_request | ...",
  "targets": [{"type": "collector", "id": "uuid", "expected_version": 7}],
  "parameters": {"typed": "per action_type schema, extra=forbid"},
  "evidence_refs": [{"bundle_id": "uuid", "ref": "E3", "bundle_hash": "sha256:..."}],
  "resource_versions": {"collector:uuid": 7},
  "policy_version": "p3",
  "created_by": "user uuid",
  "created_at": "2026-10-20T15:00:00Z",
  "expires_at": "2026-10-20T16:00:00Z",
  "simulation": {"result_hash": "sha256:...", "run_at": "...", "preconditions": ["..."]},
  "verification": {"checks": ["collector heartbeat < 90s within 5m"], "timeout_s": 300},
  "recovery": {"strategy": "none | compensate | manual", "steps": ["..."]},
  "plan_hash": "sha256 over canonical JSON of all fields above"
}
```

Rules:

- `action_type` comes from an allowlist in code. Free text is never an action. The model may propose only a `(action_type, targets, parameters)` triple that the validator accepts; the platform fills every other field.
- Target ids are explicit UUIDs resolved by the platform, never selectors like "all racks in the room". The caller must hold the permission and scope for each target.
- `resource_versions` use the existing integer `version` and `If-Match` pattern (`backend/app/application/concurrency.py` `lock_versioned_row`, `require_if_match`).
- Parameters are typed per action. No raw commands, SQL, URLs or credentials.

## 2. Plan states

```
draft -> simulated -> pending_approval -> approved -> executing -> succeeded
                 |            |               |            |-> failed
                 |            |               |            |-> outcome_uncertain
                 |            |               |-> expired
                 |            |-> rejected
                 |-> simulation_failed
approved/pending_approval -> invalidated   (any change to plan, target version, policy, or evidence)
```

- **Rejected**: an approver declines; terminal; reason code stored.
- **Expired**: `expires_at` passed before execution starts; terminal. Queue delay counts: the executor refuses a plan that expires while queued.
- **Failed**: the adapter returned a definite failure; recovery rules run; terminal.
- **Outcome uncertain**: timeout, lost connection after send, or ambiguous response. No automatic retry. The executor runs the verification checks read-only, records the result, and requires a human decision. Retrying needs a new plan, because the first may have applied.
- **Invalidated**: any change to a target's `version`, the policy version, an evidence bundle hash, or the plan itself voids approval. The approver sees a diff for the new plan.

## 3. Approval binding

- An approval records `plan_hash`, approver id, approver authorization snapshot hash, time and optional comment.
- The approver is not the requester (four-eyes) for actions above the lowest risk class, and holds the permission and scope for every target.
- Approval is single-use and bound to one `plan_hash`. It cannot be replayed on a different plan or after expiry.
- Approvals are audited with `write_audit_log` in the same transaction as the state change, and published through `write_outbox_event`.

## 4. Independent execution recheck

The executor trusts nothing from request time. Immediately before each action it rechecks, in one transaction where possible:

1. Requester and approver still hold the required permissions and scopes (`load_effective_access`), with no explicit deny.
2. Approval exists, matches `plan_hash`, is unexpired, unused.
3. Every target's current `version` equals the plan's.
4. Global kill switch and per-action-type switch currently allow execution. Both default to disabled.
5. Change window is open (needs a change-window source; none exists, decision 7).
6. Blast radius within policy: target count, dependents from `impact_service` / power graph, site and rack limits.
7. Idempotency claim (`idempotency.get_or_claim`) for `plan_id + attempt`; a lost claim means do nothing.
8. No conflicting plan on the same target (advisory lock per target, lock order by id to avoid deadlocks).
9. Simulation result still valid for current versions; stale simulation blocks execution.

A failed recheck ends in `invalidated` or `expired`, never in a best-effort run.

## 5. Execution adapters and recovery

- One adapter per action type, in its own module, holding the narrowest credential that action needs. The model process and API process never hold those credentials; the adapter runs on a separate worker queue with its own secret scope.
- Adapters expose `simulate()`, `execute()`, `verify()` and `recover()`; only `execute()` writes. All calls take the plan and return typed results.
- Outbound calls reuse the hardening of `backend/app/application/outbound_http.py` (URL validation); destinations are allowlisted per adapter.
- **Recovery is per action, not assumed.** Each action type declares one of: *reversible* (inverse action defined and tested), *compensable* (a documented compensating action with side effects), *irreversible* (requires highest approval class and is excluded from any automation), *no-op safe* (idempotent notification-style action). An action without a declared and tested class cannot be added to the allowlist.
- Post-change verification reads the target through normal read services and compares with the plan's `verification` checks; failure triggers `recover()` if defined, otherwise `outcome_uncertain` or `failed` and a human page.

## 6. Autonomy gates (M6, tests only)

M6 builds the gate logic and tests it with a simulated adapter. Production autonomy remains disabled. A future gate for one action type requires, at minimum: owner authorization in writing, a record of N human-approved executions with zero unrecovered failures, independent red-team acceptance, a rate and blast-radius cap, and a kill switch tested in the previous 30 days. No autonomous action type exists or is proposed by M1.

## 7. Threat model

| # | Threat | Vector | Controls | Required test |
|---|---|---|---|---|
| T1 | Direct prompt injection | User asks the assistant to ignore rules, reveal other sites, or run a tool | Model has no authority: tool allowlist, per-tool permission and scope, validator; system prompt is not a security boundary | Forbidden tool execution; cross-scope question returns `denied` |
| T2 | Indirect prompt injection | Asset name, note, ticket, alarm subject or datasheet text carries instructions | Retrieved text is data, delimited and typed in the bundle; never concatenated into tool arguments; tool args validated by strict schemas; no tool can write | Malicious retrieved metadata test |
| T3 | Cross-site/customer disclosure | Query without scope; join across tenants; ranking over all sites | Predicate built from `ctx` before retrieval, aggregation and ranking; `omitted.count_visible_to_caller` only | Site/rack isolation; moved assets |
| T4 | Count and aggregate leakage | Totals, "top N", percentages computed over hidden rows; error text differences | Aggregates over scoped set only; identical not-found/denied shape; no row counts in errors | Aggregate/count leakage; unknown vs unauthorized id |
| T5 | Cache and history contamination | Shared cache key; conversation reuse; provider prompt cache | Keys include `user_id` + `permission_hash`; per-user conversation rows; revalidation on read | Cross-user cache/history reuse |
| T6 | Forged citations, hallucinated assets | Model cites nonexistent ref or invents a rack | Validator rules 1-6 (bundle membership, entity must appear in cited item, recomputation) | Fabricated reference; unsupported claim |
| T7 | Stale authorization, moved resources | Permission revoked or asset moved during async work | Revalidate before delivery and on every read; item-level visibility recheck; purge on revoke | Authorization revoked mid-request; moved asset |
| T8 | Sensitive prompts and logs | Secrets, emails or topology in prompts, logs, traces | Typed redaction before provider and audit; hash-only audit default; logs carry fixed fields (pattern from `telemetry_ledger`) | Log/audit scrape test for planted secrets |
| T9 | Unauthorized tool calls and target selection | Model picks a target outside scope; widens a selector | Explicit target UUIDs checked per caller; no selector expansion by the model | Target outside scope rejected at plan validation and at execution |
| T10 | Approval replay, expired plans, queue delay | Reuse of old approval; plan run hours later | Approval bound to `plan_hash`, single-use, expiry rechecked at execution | Replay, expiry-in-queue, changed-version tests |
| T11 | Provider outage, retries, uncertain outcome | Timeout after side effect; duplicate delivery | No model on the write path; evidence-only fallback; `outcome_uncertain` state; idempotency claim per attempt | Failure-injection on adapter timeout |
| T12 | Privilege creep through the AI permission | `assistant:use` treated as read access | Feature gate only; every tool checks its own code | User with `assistant:use` and no `alarm:read` gets `denied` for alarms |
| T13 | Cost and denial of service | Huge windows, many questions | Bounds in the M2 document section 9; per-user quotas; dedicated queue | Bound enforcement tests |
| T14 | Supply chain / provider change | Model version changes behavior silently | Pin provider model id in the response `versions`; evaluation rerun on change | Version recorded in every response |

## 8. Residual risks recorded for the owner

- Unscoped alarm/telemetry/power reads (D1) cap who may use those tools.
- No change-window or maintenance-window model exists.
- The adapter credentials design (secret store, rotation) reuses `backend/app/core/secrets.py` versioned keys; adapter-specific secret scopes are new work.
- Provider terms are undecided.
