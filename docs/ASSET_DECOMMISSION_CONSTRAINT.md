# Asset decommission timestamp guard

Issue #75's row-lock remediation shipped in PR #71. Revision
`0033_asset_decommission_guard`, following `0032_catalog_documents`, adds the remaining
database guard:

`decommissioned_at IS NULL OR lifecycle_status IN ('decommissioned', 'removed')`.

A timestamp means a recorded decommission event, stored as naive UTC under the
existing column contract. NULL means no recorded date; it does not prove that
decommissioning never occurred. Direct removal from planned, installed or reserved
may retain NULL. Existing equipment creation and bulk import may create terminal
assets with unknown dates. This change preserves those contracts and does not
fabricate dates or require a timestamp for every terminal asset.

## Preflight and deployment

Before upgrading, audit the target database (repository tests cannot establish
production data quality):

```sql
SELECT id, asset_tag, lifecycle_status, decommissioned_at
FROM managed_asset
WHERE decommissioned_at IS NOT NULL
  AND lifecycle_status NOT IN ('decommissioned', 'removed');
```

If any rows are returned, investigate their audit trail and obtain an explicit
data correction decision. Do not automatically erase timestamps or change statuses.
The migration repeats this check under an ACCESS EXCLUSIVE table lock and aborts
with a diagnostic if violations exist. It changes no data.

Schedule a maintenance window: the lock blocks reads and writes on managed_asset
until transaction completion, including validation. NOT VALID followed by VALIDATE
occurs in the same transaction; this is not an online, nonblocking migration.
Use the deployment's database lock/statement timeouts to bound waiting and retry
after resolving contention. Validation failure rolls back the transaction.

The constraint rejects future inconsistent INSERT and UPDATE operations, including
writes outside the API. PR #71's row lock remains necessary for transition and
audit/outbox correctness.

## Rollback and verification

Downgrade removes only the constraint, preserving every row and timestamp. A later
upgrade audits again. No backfill is required for conforming data, including NULL
terminal dates. Tests cover the full status/timestamp matrix, direct updates,
populated upgrade/downgrade/re-upgrade, validated constraint state, and refusal of
inconsistent historical rows without rewriting them. Existing API lifecycle and
concurrency tests remain regression coverage.
