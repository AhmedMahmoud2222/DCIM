"""The single lock that makes a delegated-administration decision valid until it commits.

An authority decision ("the actor strictly outranks the target") is computed from the
actor's and target's effective permissions and from their site/rack scope. Scope for
`rack_scope=selected` and `all` is derived from the CURRENT rack placement, so three kinds
of writers can invalidate a decision between the check and the commit:
  * authority writers (users, groups, memberships, grants)      -> take it EXCLUSIVE
  * rack-placement writers (create, move, retire, bulk import)  -> take it SHARED
Exclusive conflicts with exclusive and with shared. Placement writers do not block each
other. A placement change therefore waits for any in-flight authority decision to commit,
and an authority decision waits for any in-flight placement change to commit and then reads
its result (READ COMMITTED re-reads on the next statement).

Global lock order (always acquire left to right, never the reverse):

    AUTHORITY (this lock, X or S)  ->  ADMIN_INVARIANT  ->  row locks (job, rack, placement,
    group, user, audit/outbox inserts)

  * Authority writers take AUTHORITY(X) as their first statement, then ADMIN_INVARIANT
    (`assert_administrator_remains`), and touch only user/group/member/grant/token/audit rows.
  * Placement writers take AUTHORITY(S) inside `placement_service.move_rack` /
    `retire_rack_placement`. Their callers may already hold job/rack/idempotency row locks;
    no authority writer ever waits for those rows, so no cycle can form.
  * Power-topology (`POWER_TOPOLOGY_MUTATION_LOCK_KEY`) and catalog/alarm/discovery row locks
    are never taken in a transaction that also takes AUTHORITY.

All locks are transaction-scoped (`pg_advisory_xact_*`): they release on commit or rollback,
including a rollback to a savepoint taken before acquisition. A caller must therefore not
commit between taking the lock and finishing the work it protects.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AUTHORITY_LOCK_NAME = "dcim.authority_change"
ADMIN_INVARIANT_LOCK_NAME = "dcim.admin_invariant"


async def acquire_authority_lock(db: AsyncSession) -> None:
    """Exclusive: held by every route that decides or changes who may administer whom."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": AUTHORITY_LOCK_NAME})


async def acquire_placement_scope_lock(db: AsyncSession) -> None:
    """Shared: held by every writer that can change which site a rack currently sits in."""
    await db.execute(text("SELECT pg_advisory_xact_lock_shared(hashtext(:name))"), {"name": AUTHORITY_LOCK_NAME})
