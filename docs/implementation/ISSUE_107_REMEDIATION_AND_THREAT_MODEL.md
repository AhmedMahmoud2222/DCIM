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
7. Execution guard of section 4a (durable intent for the stable logical action). The request-level `idempotency.get_or_claim` is **not** the execution guard: it reclaims processing claims after 30 seconds and its fencing protects only the database completion, not an external side effect. A lost or unresolved guard means do nothing.
8. No conflicting plan on the same target (advisory lock per target, lock order by id to avoid deadlocks).
9. Simulation result still valid for current versions; stale simulation blocks execution.

A failed recheck ends in `invalidated` or `expired`, never in a best-effort run.

## 4a. Durable execution intent and uncertain-outcome reconciliation (PROPOSED)

Design only. This closes the review finding that request-level idempotency cannot prevent a duplicate external send after a worker crash.

**Stable logical-action key.** `action_key = sha256(plan_hash || target_id || action_type || step_index)`. It contains no `attempt`, worker id or timestamp. Every retry, redelivery, reclaimed task or restarted worker for the same plan step derives the same key. A new attempt never obtains an unrelated guard.

**Intent record (`execution_intent`, new table).** Unique constraint on `action_key`. Columns: `action_key`, `plan_id`, `plan_hash`, `target_id`, `state` (`intent_recorded | send_started | confirmed | failed_definite | outcome_uncertain | reconciled_applied | reconciled_not_applied | abandoned`), `adapter_correlation_id` (a value sent to the external system where it supports one), `worker_id`, `lease_expires_at`, `created_at`, `updated_at`, `reason_code`. Rows are never deleted by the executor.

**Order of operations in `execute()`:**
1. In one database transaction: recheck (section 4); consume the approval (set `used_at` with a conditional `UPDATE ... WHERE used_at IS NULL`, zero rows updated aborts); insert the `execution_intent` row in `intent_recorded`; move the plan to `executing`; write audit and outbox rows. Approval consumption, the intent insert and the state transition commit or roll back together.
2. Commit. Only then compare-and-set the intent to `send_started` and persist it **before** the adapter performs the external call.
3. Call the adapter. Record `confirmed` or `failed_definite` only on an unambiguous adapter result.
4. Any state other than `intent_recorded` found by a worker means a previous worker may have sent. That worker does not send.

**Crash and reclaim rules.**
- A row in `send_started` whose lease expired, or any timeout, lost connection or ambiguous response, becomes `outcome_uncertain`. It is never reclaimed for execution, never auto-retried, and never re-sent by a Celery redelivery.
- A row still in `intent_recorded` with an expired lease proves no send began (step 2 had not committed); a worker may take the lease by compare-and-set and continue. This is the only automatic re-entry.
- `confirmed` and `failed_definite` are terminal for that key. Retrying after `failed_definite`, or after reconciliation as `reconciled_not_applied`, requires a **new plan** (new `plan_hash`, so a new `action_key`) and a new approval.

**Approved reconciliation path.**
1. The executor runs the plan's `verification` checks and the adapter's read-only `verify()` (and, where the external system supports it, a lookup by `adapter_correlation_id`). It records the evidence, and does not change state on its own.
2. A human holding the action's reconcile permission, who is not the sole requester for actions above the lowest risk class, records the decision `reconciled_applied` or `reconciled_not_applied` with a reason. The decision is audited (`write_audit_log`, same transaction) and published through `write_outbox_event`.
3. `reconciled_applied` ends the plan as succeeded-with-note. `reconciled_not_applied` allows only a new plan. `abandoned` closes it with no further action.
4. An `outcome_uncertain` row older than the policy age raises a page; it never expires into a retry.

**No-op safe actions** (idempotent notification-style actions) still record intent. Resending requires a new plan, because a duplicate notification is a visible side effect.

**Acceptance tests (fail-closed, mutation-sensitive).**
- **A1 crash after side effect:** the adapter fake records the send, then the worker process is killed before `confirmed` is written. After lease expiry, the Celery task is redelivered and the claim in `idempotency` is reclaimed after 30 s. Expected: the fake observes exactly one send; the row is `outcome_uncertain`; the plan is not `executing`; a page is raised.
- **A2 crash before send:** kill after step 1 and before `send_started`. Expected: a second worker continues and the fake observes exactly one send.
- **A3 stable key across attempts:** two attempts for the same plan step produce the same `action_key`; a different `plan_hash` or `target_id` produces a different one. Mutation check: adding `attempt` to the key must fail this test.
- **A4 concurrent workers:** two workers race on one key; exactly one reaches `send_started`.
- **A5 approval atomicity:** inject failure after approval consumption and before the intent insert; both roll back and the approval remains unused. A second consumer of the same approval updates zero rows and sends nothing.
- **A6 reconciliation:** from `outcome_uncertain`, an unauthorized user cannot reconcile; an authorized, audited decision moves to `reconciled_applied` or `reconciled_not_applied`; neither triggers a send; a retry only via a new plan and approval.
- **A7 ambiguous response and timeout:** both yield `outcome_uncertain`, not `failed_definite`.
- **A8 idempotency expiry:** the request-level claim expiring or being reclaimed never causes a send when the intent row exists.

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
| T11 | Provider outage, retries, uncertain outcome | Timeout after side effect; duplicate delivery | No model on the write path; evidence-only fallback; `outcome_uncertain` state; durable `execution_intent` keyed to a stable logical action, persisted before send (section 4a); human reconciliation, no reclaim or auto-retry | Failure-injection on adapter timeout; crash-after-side-effect tests A1-A8 (section 4a) |
| T12 | Privilege creep through the AI permission | `assistant:use` treated as read access | Feature gate only; every tool checks its own code | User with `assistant:use` and no `alarm:read` gets `denied` for alarms |
| T13 | Cost and denial of service | Huge windows, many questions | Bounds in the M2 document section 9; per-user quotas; dedicated queue | Bound enforcement tests |
| T14 | Supply chain / provider change | Model version changes behavior silently | Pin provider model id in the response `versions`; evaluation rerun on change | Version recorded in every response |

## 8. Residual risks recorded for the owner

- Unscoped alarm/telemetry/power reads (D1) cap who may use those tools.
- No change-window or maintenance-window model exists.
- The adapter credentials design (secret store, rotation) reuses `backend/app/core/secrets.py` versioned keys; adapter-specific secret scopes are new work.
- Provider terms are undecided.
