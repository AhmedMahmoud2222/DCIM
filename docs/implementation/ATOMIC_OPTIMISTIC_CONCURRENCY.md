# Atomic optimistic concurrency (If-Match)

## Defect

Six version-guarded mutations loaded the row, compared `If-Match` to `row.version` in Python, mutated the ORM
object, incremented `version` and committed. Under PostgreSQL READ COMMITTED two requests could both read
version N, both pass the check and both commit N+1 (lost update, duplicate version, misleading audit/outbox).

| Route | Handler |
|---|---|
| `PATCH /integrations/{id}` | `update_integration()` |
| `PATCH /racks/{id}` | `update_rack()` |
| `PATCH /locations/rooms/{id}` (`/rooms/{id}`) | `update_room()` |
| `PATCH /equipment/{id}` | `update_equipment()` |
| `PATCH /power/connections/{id}` | `update_power_connection()` |
| `POST /floor-plans/{id}/activate` | `activate_floor_plan()` |

## Fix

`app/application/concurrency.py::lock_versioned_row()` runs `SELECT ... FOR UPDATE` (with `populate_existing`
so a stale identity-map copy is refreshed) and only then compares `If-Match`. The loser blocks until the winner
commits, reads the new version and receives 409. This is the primitive `lock_draft_revision_for_edit()` already
uses for catalog revisions. Authorization, before/after audit data, outbox writes and the single transaction are
unchanged.

### FloorPlan activation

The invariant "one active plan per room" spans rows, so locking the plan alone is not enough: two different plans
in one room would each lock only themselves. Activation now:

1. reads the plan's room and the room's currently active plan (the state the caller acted on);
2. takes `FOR NO KEY UPDATE` on the `room` row (does not block FK checks from plan inserts);
3. locks the plan and compares `If-Match` (409 if stale: same plan activated twice);
4. locks the room's active plan; if it differs from step 1, answers 409 (a different plan was activated while this
   request waited);
5. supersedes the prior plan and activates, as before.

`uq_floor_plan_one_active_per_room` stays as defense in depth.

## Scope review

Other users of `require_if_match`, `check_version_match` and `version += 1` were checked and left unchanged because
they already lock the row: catalog revision/child/document/extraction paths (`lock_draft_revision_for_edit`),
power capacity replacement (`FOR UPDATE`), bulk-import rack/equipment update commit (`with_for_update`), and
placement moves (`placement_service`).

## Tests

`tests/integration/test_atomic_optimistic_concurrency.py`: a separate connection holds the lock while two
independent-session requests send the same `If-Match: N`; the test waits until PostgreSQL reports both blocked,
releases the lock, and asserts one 200 and one 409, version incremented once, the winner's values, and
audit/outbox rows for the committed mutation only. Existing sequential stale-version tests remain.
