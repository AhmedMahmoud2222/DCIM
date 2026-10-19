# Issue 128 G8: unresolved legacy alarm rules (DRAFT)

Integration baseline: cloud main `bd12390544164132df90002c3ea40119198db2cb`.
Owner authorization: issue 128 comment 6108192722; source design 6100092674.
Do not merge, deploy, mark ready, or close issue 128 until G4-R is merged and
independently accepted and the owner approves release.

## Contract

An enabled legacy rule with both `unit` and `registry_version` NULL skips
evaluation and preserves valid telemetry ingestion. Other evaluable rules still
evaluate. No threshold unit is guessed from readings or source mappings.
Existing inferred legacy rules (unit present, version NULL) keep their authored
threshold and conversion behavior. Genuinely incompatible units and malformed
versioned rules retain the existing rejection behavior.

The first skipped reading sets `blocked_notified_at` to that reading's central
receipt timestamp. In the reading transaction, one audit row
`alarm.rule.evaluation_blocked` and one `AlarmRuleEvaluationBlocked` outbox row
record the block episode. The marker limits the structured warning to once per
episode. Failed transactions can repeat their warning, but roll back the marker,
audit and event. Rule rows are locked in UUID order and held through commit;
this serializes concurrent first observations and operator updates. Row loading
uses `populate_existing` so a waiter cannot evaluate stale ORM identity state.

### Independent review correction: request-wide lock order

Ordering only each individual reading's rules is insufficient: opposite-order
multi-integration/metric batches retain their earlier locks through commit and
can deadlock. Before the first record savepoint, the collector batch now locks
its complete existing enabled rule set in one global UUID order. Both the
eligibility query and rule-lock query filter the real current
`CollectorAssignment` for this authenticated collector; the rule query takes
`FOR UPDATE OF alarm_rule` only. Foreign/unassigned integration rules are not
locked. There is no global advisory lock or retry that hides partial work.

The rule ID snapshot stays in the request's session until commit and is removed
in `finally`. Every later per-reading rule query is restricted to that snapshot;
new rules inserted concurrently begin participating next request, avoiding new
out-of-order locks. Records retain their original order, individual savepoints
and ACK order. Assignment is still rechecked per record; integrations not owned
at initial eligibility are rejected for this request, even if newly assigned
mid-request. This fixes the new alarm lock ordering without changing G4 paths.

`backend/app/api/v1/telemetry.py` is now an additional G8 edit. G7-readings also
touches this file: reconcile the import and batch wrapper with its additive
history/API changes during eventual rebase; do not discard either change.

**Open deployment decision:** who watches the attention list, who receives
`AlarmRuleEvaluationBlocked`, and which notification policy/channel routes it.
This prototype persists the event; it does not invent a recipient or channel,
and it does not claim that existing notification workers deliver the event.

## Operator API

| Route | Permission | Behavior |
|---|---|---|
| `GET /api/v1/alarms/rules?needs_attention=true` | `alarm:read` | Enabled unitless rules, explicit status, resolution metadata and update timestamp. Optional integration filter; limit 1–500 and offset. |
| `POST /api/v1/alarms/rules/{id}/resolve-unit` | `alarm:manage` | `{unit, reason, expected_updated_at}`. Resolve a legacy unitless rule against registry v1 dimension; preserve threshold; audit and outbox. |
| `POST /api/v1/alarms/rules/{id}/disable` | `alarm:manage` | `{reason, expected_updated_at}`. Disable under lock; audit and outbox; existing alarm lifecycles remain unchanged. |

Requests require a nonblank reason of at most 2048 characters and an aware
timestamp. Stale updates return 409; wrong dimension 422; missing rule 404;
missing permission 403. A repeated operator resolution to the same unit is
idempotent even with the original timestamp, and adds no second audit/event.
A different unit conflicts. A repeated disable is also idempotent.

List statuses are derived: `unresolved`, `inferred_legacy`, `explicit`, and
`invalid_contract` for malformed versioned rules lacking a unit. The last status
is visible for separate remediation; it does not auto-repair a damaged contract.

No retroactive evaluation occurs. The resolution response returns
`retained_blocked_window_readings`, counting retained raw readings for that
integration/metric by central receipt timestamp between the first blocked
receipt and resolution. `blocked_window_count_complete=false` is always
explicit: retention/compaction may have removed readings, and this is not a
durable per-rule skipped-evaluation counter. Idempotent retries may show a lower
retained count after retention runs. This avoids fabricating lost history.

These endpoints deliberately use the existing create/list alarm permission
boundary. Any pending RBAC scope work must be reconciled before readiness; this
prototype changes no authorization-engine files and does not claim scoped
permission enforcement beyond the current baseline.

## Migration and rollback gate

`0055_alarm_rule_resolution` adds four nullable columns without defaults or
backfill: resolution actor (FK to user, ON DELETE SET NULL), timestamp, reason,
and first-block marker. Populated downgrade refuses while any column has
evidence, holding an ACCESS EXCLUSIVE lock across the check/removal to prevent
a concurrent writer slipping between them. No existing rule unit is rewritten.

**Reconciled chain:** 0055 follows merged G6's
`0051_aggregate_lineage`. A regression reads the complete Alembic graph
and requires exactly this single head. Sibling migrations in other Draft PRs
must be reconciled before their eventual integration.
Rehearse populated upgrade, FK behavior, clean downgrade, populated refusal and
lock behavior on a disposable PostgreSQL test database after reconciliation.

### Hold-release lock order

The published draft omitted the recovered rule-first hold-release correction.
The release route now reads the hold's integration identity without locking,
locks its enabled rules in UUID order, then locks and reloads the hold before
ingestion. Its bounded rule snapshot is removed on success and exceptions.
This matches collector batches' rule-before-hold/aggregate/ledger hierarchy;
G4-R's replay-horizon refusal and savepoint behavior remain intact. Unit lock
ordering tests run locally; real two-session PostgreSQL acceptance is pending.

## Validation and outstanding gates

Unit tests cover real service behavior and audit/outbox objects, unchanged
legacy/canonical comparisons, ASGI routes and permission dependencies, stale
updates, invalid dimensions/reasons, idempotence, migration additive contract
and downgrade guards. Isolated route tests override database/auth dependencies;
they do not establish database row-lock correctness or real RBAC integration.

`tests/integration/test_unresolved_alarm_rules.py` contains real PostgreSQL
tests for accepted batch storage, other-rule evaluation and dedup, one event
under concurrent first observations, competing resolutions, next-reading
evaluation without replay, and transactional rollback after an outbox failure.
They have not run in the available environment. The standard backend conftest
truncates database tables; do not run it against application databases. Use a
dedicated disposable test database with the reconciled migrations applied.
The second integration file, `test_alarm_rule_resolution_migration.py`, adds
populated downgrade/upgrade preservation and populated evidence refusal
rehearsals. A tenth G8 integration case now exercises two real sessions sending
opposite integration/metric orders, using events to coordinate their overlap
and asserting ACK order, rejected foreign integration, stored readings and one
event per block episode. None has executed.

Local evidence after review correction: 46 scoped alarm tests passed; the new
three regression cases cover prelocking before records, foreign-rule exclusion,
ACK order, restricting later rule locks to the snapshot, and cleanup on error.
Lint of the changed Python files and mypy of the changed application modules
passed. A broader
unit-directory run with database fixtures intentionally excluded yielded
128 passed and three setup errors in existing collector authentication tests
requiring `db_session` (`test_valid_signature_authenticates`,
`test_tampered_body_rejected`, `test_wrong_secret_rejected`), then stopped at
maxfail=3. This is not a full suite pass. Standard PostgreSQL/full-suite/CI
results remain unavailable. No database, Redis service, or user Docker setup
was contacted by these local checks.

Required before readiness: real PostgreSQL tests and populated migration
rehearsal, full repository tests, CI/deployment validation, independent
non-author review, G4 completion, and owner release approval. A read-only
operator rules panel (`AlarmRulesPage`, gated on `alarm:read`/`alarm:manage`)
is included so unresolved legacy rules can be resolved or disabled; the
authorization engine is unchanged.
