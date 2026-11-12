# Issue #337 — principal-scoped idempotency ledger

**Status: implementation pending. This file is an implementation handoff, not a security fix.**

Owner issue: https://github.com/AhmedMahmoud2222/DCIM/issues/337

## Verified defect
The idempotency ledger identifies records only by `(key, endpoint)`. Different authenticated principals sharing a key and endpoint can interact through a shared record; this must be isolated even when a key is exposed.

## Implementation contract
- Bind each idempotency claim, lookup, replay and stale reclaim to authenticated principal identity, not only the client-supplied key.
- Apply a safe schema migration, including a considered policy for unexpired legacy records and collector behavior.
- Propagate a trustworthy principal from every endpoint calling `get_or_claim`, including equipment, racks, PDUs, managed assets, and collector ingestion.
- Retain atomic claim, stale-owner fencing, release, retries and conflict behavior.
- Regression tests: distinct principals with identical keys and bodies cannot replay each other's responses; different bodies do not block each other; same-principal retries deduplicate; migration rollback and concurrency work.

## Security acceptance gate
- Tests on PostgreSQL across two real authenticated principals.
- No cross-principal replay or denial of service through key collision.
- Exact-head CI green and independent review on **this PR**, with review comments.
- No production migration or merge authorized by this planning-only commit.
