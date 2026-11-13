# Issue #337 — principal-scoped idempotency ledger

**Status: implemented on this branch; awaiting exact-head CI and independent review. Not merged, not deployed.**

Owner issue: https://github.com/AhmedMahmoud2222/DCIM/issues/337

## Verified defect
The idempotency ledger identified records only by `(key, endpoint)`. Different authenticated principals sharing a key and endpoint interacted through a shared record: B could replay A's stored response, or be refused with 422/503 purely because A used the key.

## Implementation
- `idempotency_key.principal` (NOT NULL, no default) and unique index `(principal, key, endpoint)` — `backend/app/domain/idempotency/models.py`, migration `0059_idempotency_principal`.
- `get_or_claim(..., principal=...)` is required; claim INSERT, lookup, replay, stale reclaim and the retry-after-release path are all principal-scoped (`backend/app/application/idempotency.py`). Empty principal raises `ValueError`. `complete_claim` / `release_claim` act on the claim row id + generation, so fencing is unchanged and cannot cross principals.
- Principal is server-derived only: `user:<users.id>` from the authenticated `AuthContext` (equipment ×2, racks, managed assets, PDU instantiate) and `collector:<collectors.id>` from the HMAC-verified collector (collector ingest). Never taken from a header or body.

## Legacy-record policy (migration 0059)
- `collector_ingest` rows: owner recovered from the `<collector_id>:<dedup_key>` key → `collector:<id>`, so collector retries keep deduplicating across the upgrade.
- All other unexpired rows: stamped `legacy:unscoped`, which no caller can produce. They are never replayed to or block anyone and age out at the 24 h TTL; worst case a client retrying across the upgrade repeats one write.
- Downgrade keeps one row per `(key, endpoint)` (completed first, then newest) before restoring the narrow unique index.

## Production hold
Migration 0057 is untouched. 0059 only revises 0058 and is subject to the same deployment gate; no migration was executed against any environment.

## Tests
- `backend/tests/integration/test_idempotency_principal_scope.py` — two real logged-in users on PostgreSQL (HTTP) + service-level claim/stale/fencing/release per principal.
- `backend/tests/integration/test_idempotency_principal_migration.py` — populated upgrade/backfill, uniqueness, downgrade dedup, re-upgrade.
- Existing fencing/concurrency tests updated for the new required `principal`.
