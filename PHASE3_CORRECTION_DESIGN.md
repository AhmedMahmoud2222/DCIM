# Phase 3 Correction Design

**Design only. No application code, tests, migrations, or configuration were
changed to produce this document.** Everything below is a proposal for
human review before implementation. Commit audited:
`4606810586084511a6b3992050baca63c5aedbf6`. Source references throughout
are to files as they exist at that commit; none were modified.

---

## Part 1 — Current Implementation, Reconstructed

Re-read in full for this design (not assumed from the audit's summary):
`backend/app/domain/power/models.py`, `backend/app/application/power_graph.py`,
`backend/app/application/power_capacity.py`, `backend/app/api/v1/power.py`,
`backend/app/api/v1/dashboard.py`, `backend/app/application/concurrency.py`,
`backend/app/application/idempotency.py`, `backend/app/core/errors.py`,
`backend/migrations/versions/0006_phase3_power_topology_and_capacity.py`,
plus the Phase 1 idempotency correction record in
`PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md` §7 (NEW-1's original writeup).

Key facts this design depends on, confirmed by direct reading:

- `PowerConnection` mutation happens through exactly three code paths:
  `create_power_connection`, `update_power_connection` (PATCH, If-Match
  gated), `disconnect_power_connection` (FOR UPDATE gated). Only *create*
  can introduce a cycle; PATCH only touches non-topological fields
  (`connection_type`/`phase`/`voltage`/`rated_current_a`/`status`);
  disconnect only removes edges, which cannot create a cycle.
- `lock_node_pair_in_canonical_order` takes `SELECT ... FOR UPDATE` on
  exactly the two `PowerNode` rows named in the new edge, sorted by
  `str(uuid)`. No other row, in `power_node` or `power_connection`, is
  locked by this function.
- `assert_would_not_create_cycle` calls `get_downstream_node_ids`, which
  reads (not locks) `power_connection` rows via ordinary `SELECT`, batched
  per BFS level, bounded by `MAX_TRAVERSAL_DEPTH=500` /
  `MAX_TRAVERSAL_NODES=5000`.
- The engine is created with no explicit isolation level
  (`backend/app/db/session.py`), so it runs at PostgreSQL's default
  `READ COMMITTED`.
- No `Idempotency-Key` handling exists on `POST /power/connections` today
  — confirmed by grep, zero matches for `Idempotency-Key`/`get_or_claim` in
  `power.py`. This matters for Part 13 below.
- `require_if_match`/`parse_if_match` (`concurrency.py`) is the one shared,
  already-hardened `If-Match` parser in the codebase; `set_node_capacity`
  in `power.py` (line 680) does not use it.

Nothing in this reconstruction contradicts the hostile audit's own
findings; this design proceeds from them as established fact, re-verified
against the source rather than assumed.

---

## Part 2/3 — The Required Invariant and Proof Obligation

**Required invariant:** at every point where any transaction commits, the
set of `PowerConnection` rows with `effective_to IS NULL` forms a directed
acyclic graph over `PowerNode` vertices.

This must hold under: single-transaction creation (already correct — the
existing self-loop CHECK plus a same-transaction traversal handles this
trivially, no concurrent actor involved); two concurrent creations sharing
an endpoint (already correct today — canonical locking on the shared
endpoint serializes these, confirmed by the existing passing concurrency
test); two concurrent creations on **disjoint** endpoint pairs that jointly
close a cycle through pre-existing edges (**confirmed broken today** —
Section 9 of the hostile audit, reproduced again for this design in Part 5
below); three or more concurrent creations (analytically extends the
disjoint-pair case — not independently executed in this design pass,
flagged as a red-team follow-up); creation racing a concurrent disconnect
(analytically safe: removing an edge can only shrink the reachable set,
never introduce a cycle — not independently executed here); creation
immediately following another transaction's commit (ordinary READ
COMMITTED already handles this correctly, since the committing
transaction's write becomes visible to the next transaction's fresh
`SELECT`); rollback/abort (by definition removes the aborting
transaction's edge from consideration, cannot contribute to a cycle);
retries (must be proven not to introduce a *duplicate* edge or a
resurrected cycle — addressed per-option below).

The proof obligation for any candidate fix is: **no interleaving of two or
more concurrent connection-creation transactions can result in a committed
graph that contains a cycle no single one of those transactions would have
accepted in isolation.** This is a safety property (nothing bad ever
happens), not a liveness property (something eventually happens) — the
options below are compared primarily on whether they establish safety, and
secondarily on what they cost for liveness (throughput, retry burden).

---

## Part 4 — Option Comparison

### Option A — PostgreSQL SERIALIZABLE / SSI

**Mechanism:** run every connection-creation transaction at
`SERIALIZABLE` isolation. PostgreSQL's Serializable Snapshot Isolation
(SSI) tracks read/write dependencies between concurrently-active
serializable transactions and aborts one of them (with SQLSTATE `40001`)
whenever it detects a "dangerous structure" — a cycle of rw-antidependencies
among concurrent transactions — that could not have occurred under any
serial execution order.

**Does it prevent the demonstrated race?** **EXECUTED — VERIFIED.** See
Part 5 for the experiment. Result: across 5 trials, one of the two
transactions was reliably aborted with SQLSTATE `40001` before either
committed a cycle; the other committed cleanly; the final graph was never
cyclic in any trial.

**Why it works (analytical):** Tx1's cycle-check read (`SELECT ... WHERE
source_node_id IN (frontier)`, walking from N2) has a read dependency on
whatever rows currently satisfy `source_node_id = N3` — if Tx2's insert
(`source_node_id = N3, target_node_id = N4`) had already been visible, Tx1
would have read a different result set (one that reaches N4, and via
N4→N1, closes the check against N1). Symmetrically, Tx2's read (walking
from N4) has a read dependency on whatever satisfies `source_node_id = N1`
— if Tx1's insert had already been visible, Tx2 would read differently.
Both transactions are concurrent (neither commits before the other starts
its read), so both dependencies exist simultaneously: a rw-antidependency
cycle among two concurrently-active serializable transactions is exactly
the "dangerous structure" SSI is designed to detect and break.

**Exact transaction boundaries required:** the entire
`lock+cycle-check+insert+commit` sequence in `create_power_connection`
must run inside a single transaction opened with `SERIALIZABLE` before any
statement in it executes (SQLAlchemy: `await
session.connection(execution_options={"isolation_level":
"SERIALIZABLE"})` before the first statement, or set it at the engine/
session-factory level for this specific code path — not globally, since
SERIALIZABLE has a real throughput cost and most Phase 3 endpoints do not
need it).

**Are the current cycle-check queries sufficient under SERIALIZABLE?**
**EXECUTED — VERIFIED, yes**, with no query changes required. The existing
`SELECT` statements in `_traverse` are exactly what SSI needs to see to
build its predicate-lock dependency graph; nothing about them needs to
change for SERIALIZABLE to work.

**Retry requirement:** yes — mandatory. A `40001` is PostgreSQL's explicit
contract that the transaction must be retried from its start (not
resumed), because its reads may already reflect a state that is no longer
valid. The API layer must catch `sqlalchemy.exc.DBAPIError` where
`exc.orig.sqlstate == "40001"`, roll back, and re-run the entire
`create_power_connection` body (fresh locks, fresh cycle check, fresh
insert attempt) from scratch, not merely retry the `commit()` call.

**How many retries, and what happens if all fail:** recommend a small
bounded retry count (3, matching a common Postgres SSI-handling
convention) with no backoff needed for this specific contention pattern
(the conflict resolves itself the instant the other transaction commits,
which — per Part 5's ~80ms timings — is fast; this is not a long-held-lock
scenario needing exponential backoff). If all retries are exhausted (only
possible under sustained, unusually high contention on the *exact* same
disjoint-pair pattern, which is intrinsically rare), return `503 Service
Unavailable` with a `Retry-After` hint — never a `500`, and never silently
drop the request.

**Deadlock interaction:** SSI aborts via serialization failure, not
Postgres's classic deadlock detector, for *this specific* race (the
existing `FOR UPDATE` canonical-order locking already prevents the classic
opposite-order deadlock independently, unchanged by adding SERIALIZABLE on
top). A deadlock (`40P01`) remains theoretically possible if some other,
unrelated code path acquires `power_node` locks out of canonical order;
Option A does not change that risk surface, and the retry handling above
should catch `40P01` alongside `40001` (same retry treatment, different
log label).

**Connection-pool implications:** SERIALIZABLE transactions must hold
their connection for the full lock+check+insert+commit sequence exactly
as READ COMMITTED does today — no additional pool pressure from the
isolation level itself, only from retries (bounded, per above).

**Interaction with idempotency:** if `POST /power/connections` ever gains
an `Idempotency-Key` (it does not today), the *entire*
claim→write→complete sequence, not just the write, would need to be
inside the retry loop, because a `40001` mid-transaction means the
idempotency claim itself (if taken inside the same transaction) is rolled
back too — this is analytically identical to how `get_or_claim`/
`complete_claim` already require same-transaction atomicity (Part 1's
`idempotency.py` docstring), just with one more failure mode
(serialization abort) added to "the write failed, release and let a retry
reclaim."

**Interaction with audit/outbox:** unaffected — both are already written
in the same transaction as the domain write and thus roll back together
with it on a `40001` abort, exactly as they already roll back together on
any other exception today. No new dual-write or partial-commit risk is
introduced.

**Operational observability:** a `40001` should be logged as a distinct,
named, expected event (`power_connection_create_serialization_conflict`),
not as an error — see Part 12.

**Expected throughput impact:** SERIALIZABLE has a real, measurable
overhead per transaction (predicate lock bookkeeping) versus READ
COMMITTED, and — more importantly for *this specific* endpoint — it must
be applied broadly enough to catch every transaction that could
participate in a cycle-closing race, meaning **every** connection-creation
transaction site-wide pays this cost, not just the two that happen to
collide. **NOT EXECUTED — no throughput benchmark was run in this design
pass**; this is a reasonable expectation from Postgres's own documented
SSI cost characteristics, not a measured number for this schema.

### Option B — Global PostgreSQL Advisory Lock

**Mechanism:** every connection-creation transaction acquires a single,
fixed-key transaction-scoped advisory lock
(`SELECT pg_advisory_xact_lock(<constant>)`) as its first statement, before
the existing canonical node-pair lock and cycle check. The lock is held
until the transaction commits or rolls back (Postgres releases
transaction-scoped advisory locks automatically at transaction end — no
manual unlock call is needed or safe to rely on for cleanup).

**Does it prevent the demonstrated race?** **EXECUTED — VERIFIED.** See
Part 5. Across 5 trials: the second transaction's advisory-lock
acquisition blocked until the first committed (observed wait ≈ 77ms,
matching the first transaction's simulated 50ms of held-lock work plus
overhead); the second transaction's cycle check then correctly saw the
first transaction's just-committed edge and rejected itself with the
*existing* `WouldCreateCycle` exception — the same clean 422 the API
already returns today for the trivial single-pair case. Zero
serialization failures, zero retries, in all 5 trials.

**Exact lock key:** a single fixed 64-bit integer constant scoped to this
one purpose (e.g. a new named constant
`POWER_TOPOLOGY_MUTATION_LOCK_KEY` in `power_graph.py`, analogous to how
`MAX_TRAVERSAL_DEPTH` is already a named module constant there) — not
derived from any node ID, precisely because the whole point is to
serialize *every* connection-creation transaction against every other one,
regardless of which nodes they touch.

**Transaction-scoped vs. session-scoped:** transaction-scoped
(`pg_advisory_xact_lock`, not `pg_advisory_lock`) is correct and required
— it self-releases on commit/rollback/connection loss, so a crashed
worker or an unhandled exception can never leave the lock held forever
(session-scoped locks require an explicit unlock call and would leak on
any code path that forgets it, including every exception path this
endpoint already has).

**Collision scope:** global to this one lock key — every
connection-creation transaction, anywhere in the graph, serializes against
every other one. This is a deliberate, broad scope; see the throughput
discussion below for why that is likely acceptable here.

**Lock acquisition ordering:** trivial — there is only one lock key, so
there is no ordering question (unlike the canonical two-endpoint lock,
which needed sorting specifically because there are two of them).

**Failure behavior:** blocking, not failing — a transaction waiting on the
advisory lock simply waits (Postgres's `pg_advisory_xact_lock` blocks
until available; there is also a non-blocking `pg_try_advisory_xact_lock`
variant, not recommended here since a request the API can process, just
slightly delayed, is a better experience than an immediate rejection for a
resource contention this brief).

**Deadlocks:** none possible from this lock alone (a single global key
cannot participate in a lock-ordering deadlock with itself); the existing
canonical node-pair `FOR UPDATE` locks are acquired strictly *after* the
advisory lock in every code path, so there is no new "lock A before B in
one path, B before A in another" hazard introduced.

**Whether all active-edge mutations must use the same lock:** yes — this
is the critical discipline requirement. `create_power_connection` is the
only mutation that can *introduce* a cycle, so it is the only one that
strictly needs the lock for correctness today, but see Part 15's
implementation-sequence note: any *future* code path that can insert a
`PowerConnection` row (a bulk-import endpoint, a data migration script, a
"clone this topology" feature) MUST also acquire this same advisory lock
before its own cycle check, or it silently reopens exactly this race
through the new path. This is a discipline that has to be enforced by
convention/code review/a shared helper function, not by the database
itself.

**Can direct SQL bypass it?** Yes — like every application-level
concurrency control in this codebase (canonical node locking, If-Match
version checks), a raw `INSERT INTO power_connection` issued outside the
application's own code path bypasses the advisory lock entirely, exactly
as it already bypasses the existing canonical-order locking today. This is
not a new gap Option B introduces; it is the same DB-vs-API-vs-service
distinction the hostile audit already drew for other invariants (F-M3,
Section 7). It is out of scope to close via triggers in this design
(triggers could enforce cycle-freedom directly in the database, which
would be a stronger but far more invasive redesign — noted in Option C as
the "why not just do this in the database" question, and rejected there
for the same complexity reasons).

**Connection pooling:** an advisory lock held for the duration of one
connection-creation request holds that one pooled connection for the same
span it already holds it today (lock acquisition, cycle check, insert,
commit) — no new connection is opened or held beyond what the endpoint
already uses. Under sustained high concurrency on this one endpoint,
requests queue waiting for the lock rather than failing, which trades
latency for correctness — acceptable given connection creation is not
expected to be a high-frequency, latency-sensitive operation (it is
provisioning-time activity, not a request-path hot loop like inventory
listing or the dashboard).

**API semantics / error translation required:** **none beyond what
already exists.** This is Option B's most significant practical advantage
over Option A: the existing `WouldCreateCycle` → 422 mapping in `power.py`
already handles the "rejected because it would create a cycle" case
correctly; Option B makes that check's answer *always correct* under
concurrency (because it can no longer run against a stale, pre-collision
view of the graph), rather than requiring a *new* error path
(`40001` → retry-or-503) the way Option A does.

**Retry requirement:** none for correctness (Option B never produces a
false "no cycle" answer that has to be caught after the fact) — the only
"retry" that already exists is Postgres's own lock queue, which the
application does not need to observe or handle specially.

**Interaction with idempotency:** cleaner than Option A — since there is
no serialization-failure/retry cycle to reason about, a future
`Idempotency-Key`-bearing version of this endpoint would only need the
already-established `get_or_claim`/`complete_claim`/`release_claim`
pattern, with no additional interaction to design for.

**Interaction with audit/outbox:** unaffected, same reasoning as Option A
— both already commit atomically with the domain write inside the one
transaction that now also holds the advisory lock.

**Performance impact:** **EXECUTED — VERIFIED** at the scale tested: lock
acquisition overhead when uncontended was ≈3ms; the observed serialization
under contention (≈77ms wait for a competing transaction doing ≈50ms of
simulated work) is exactly the expected cost of *fully* serializing this
one operation — every concurrent connection-creation request pays for
every other one's full duration, system-wide. **NOT EXECUTED** at higher
concurrency (10+ simultaneous connection-creation requests) or at realistic
production request rates for this endpoint — this design pass tested
correctness under 2-way contention, not throughput under load.

**Expected contention scope at 1,000 / 10,000 / 100,000 active nodes:**
**ANALYTICAL CONCLUSION** — the advisory lock's cost is a function of how
often connections are *created concurrently*, not how many nodes or
connections already exist (the lock itself is O(1) to acquire/release
regardless of graph size; the cycle check it protects is still bounded by
`MAX_TRAVERSAL_NODES` exactly as today). At any of the three stated
scales, if connection creation remains a low-frequency, mostly-sequential
provisioning activity (the expected real usage pattern — wiring up a new
rack's power feeds is a discrete, human-paced task, not a bulk hot loop),
full serialization of this one operation is very unlikely to be a
perceptible bottleneck. If a future bulk-import feature needs to create
many connections concurrently and finds this lock a genuine throughput
ceiling, that is the point at which Option C's finer-grained locking might
become justified — not before, per the master prompt's own "measure
before optimizing" instruction.

### Option C — Broader Deterministic Graph-Closure Locking

**Mechanism (as sketched, not implemented):** before running the cycle
check, discover and lock the *entire* downstream transitive closure of the
proposed target plus the entire upstream transitive closure of the
proposed source (in canonical order), so that no concurrent transaction
touching any node in either closure can proceed until this one finishes.

**Analytical assessment (NOT EXECUTED as a working prototype — reasoned
through only):**

- **Discovering the closure requires an initial unlocked read**, which is
  itself racy: between "read the closure" and "lock every node found," a
  concurrent transaction could add a new edge that extends the closure
  (a phantom edge), meaning the just-acquired lock set might not actually
  cover every node the cycle check needs to be safe against. Closing this
  requires either a loop ("lock what you found, re-check for growth,
  lock any new nodes, repeat until stable" — unbounded in the worst case
  against an adversarial or simply fast-growing concurrent workload) or
  Postgres-level predicate locking (which is precisely what SERIALIZABLE
  already gives you "for free," making a hand-rolled version of this
  strictly worse than Option A for the same guarantee).
- **Lock explosion:** for a node near the root of a large, well-connected
  topology (a site's single utility intake, say), the downstream closure
  could be the *entire* topology — locking thousands of `PowerNode` rows
  for the duration of a single connection-creation request, which would
  make every other topology mutation across the *whole site* block on it,
  a far worse contention profile than Option B's already-broad "every
  connection-creation request blocks every other one" (Option C could
  make ordinary reads or unrelated writes elsewhere in the graph block
  too, depending on what else takes row locks on those same nodes).
- **Lock ordering:** still solvable (sort the whole discovered set
  canonically, same principle as the existing two-node case) but now
  ordering potentially thousands of UUIDs per transaction, with
  proportionally more opportunity for two large, overlapping-but-not-
  identical closures to interleave their lock acquisition in a way that
  still deadlocks even under a total order, if the closures are computed
  from different, evolving snapshots (this needs a formal re-proof it is
  not obviously safe to assume the two-node case's proof generalizes).
- **Does it actually provide a proof of correctness?** Not without solving
  the phantom-discovery problem above, which in turn either reduces to
  Option A (use Postgres's own SSI) or introduces new unbounded-retry
  complexity. **Recommendation: reject Option C.** It is not recommended
  merely because it "sounds more granular" — on inspection, it is *more*
  complex than Option A for a *weaker* guarantee (still racy at discovery
  time) and *worse* for contention than Option B (broader lock footprint,
  not narrower, for any node with a non-trivial closure) in the topologies
  this system is actually meant to model.

### Option D — Combined Approaches

The two viable options (A and B) are not naturally combined for *this*
specific defect — they are two different, each individually sufficient,
solutions to the same problem, and applying both would only add Option
A's retry-handling complexity on top of Option B's already-sufficient
serialization for no additional correctness benefit. **Recommendation:
choose one, not both, for connection creation specifically** (Part 6). A
combined posture is worth keeping in mind for *other* Phase 3 concurrency
surfaces the audit did not flag as broken (e.g., the existing
`PowerCapacity` PUT/PATCH If-Match path is already correct with plain
optimistic concurrency and needs neither SERIALIZABLE nor an advisory
lock) — the two mechanisms are not mutually exclusive at the codebase
level, just not both needed for this one fix.

---

## Part 5 — Controlled Experiments Actually Performed

All three experiments below reused the identical scenario from the
hostile audit: pre-existing committed edges N2→N3 and N4→N1; concurrent
Tx1 (N1→N2) and Tx2 (N3→N4); forced interleaving via `asyncio.Event`
handshakes so both transactions complete their own cycle check before
either attempts to commit. Run against the real `dcim_test` PostgreSQL 16
database used by the existing test suite. All temporary scripts were
deleted after use (confirmed by `git status` in Part-18's git-safety
check).

**Experiment 1 — Baseline reconfirmation (EXECUTED — VERIFIED):** re-ran
the exact hostile-audit scenario unmodified (READ COMMITTED, existing
canonical-order locking only, no candidate fix) to reconfirm the defect
still reproduces identically before testing fixes against it. Confirmed:
both transactions' individual cycle checks passed, both committed, and
—using this design's corrected verification method (checking that *both*
new edges are actually present, not the reachability shortcut the original
audit script's first draft mistakenly used before self-correcting)— the
resulting graph contained both `N1→N2` and `N3→N4`, i.e. the true 4-cycle.

**Experiment 2 — Option A (SERIALIZABLE), 5 trials (EXECUTED —
VERIFIED):**

```
Trial 0: tx1=ABORTED sqlstate=40001  tx2=COMMITTED  n1->n2=False n3->n4=True  cycle=False
Trial 1: tx1=ABORTED sqlstate=40001  tx2=COMMITTED  n1->n2=False n3->n4=True  cycle=False
Trial 2: tx1=ABORTED sqlstate=40001  tx2=COMMITTED  n1->n2=False n3->n4=True  cycle=False
Trial 3: tx1=ABORTED sqlstate=40001  tx2=COMMITTED  n1->n2=False n3->n4=True  cycle=False
Trial 4: tx1=ABORTED sqlstate=40001  tx2=COMMITTED  n1->n2=False n3->n4=True  cycle=False
```

5/5 trials: no cycle committed. Note: in this specific asyncio-scheduled
experiment, Tx1 was aborted every time — this is an artifact of this
experiment's own scheduling (Tx1's `commit()` call happened to reach
PostgreSQL microseconds after Tx2's in this event loop's specific
execution order each time), **not a guarantee that Postgres always aborts
"the first" or "the second" transaction in general** — SSI's victim
selection is an internal PostgreSQL heuristic and must not be assumed
deterministic by any client-side retry logic.

**Experiment 3 — Option B (global advisory lock), 5 trials (EXECUTED —
VERIFIED):**

```
Trial 0: tx1=COMMITTED  tx2=REJECTED(WouldCreateCycle)  wall=0.086s  n1->n2=True n3->n4=False  cycle=False
Trial 1: tx1=COMMITTED  tx2=REJECTED(WouldCreateCycle)  wall=0.084s  n1->n2=True n3->n4=False  cycle=False
Trial 2: tx1=COMMITTED  tx2=REJECTED(WouldCreateCycle)  wall=0.083s  n1->n2=True n3->n4=False  cycle=False
Trial 3: tx1=COMMITTED  tx2=REJECTED(WouldCreateCycle)  wall=0.083s  n1->n2=True n3->n4=False  cycle=False
Trial 4: tx1=COMMITTED  tx2=REJECTED(WouldCreateCycle)  wall=0.082s  n1->n2=True n3->n4=False  cycle=False
```

5/5 trials: no cycle committed, and — unlike Option A — the losing
transaction failed with the *existing, already-handled* `WouldCreateCycle`
application exception, not a new database-level error class the API layer
would need to learn to translate.

**Option C:** **NOT EXECUTED** — no working prototype was built (Part 4
explains the analytical reasoning for rejecting it without one).

**Not executed in this design pass, explicitly:** 3+-concurrent-transaction
variants of the race (both experiments above are 2-transaction only);
throughput/latency under sustained concurrent load beyond the 2-way case;
behavior under a genuine Postgres deadlock (`40P01`) for either option;
interaction with a real HTTP-layer retry loop (both experiments called
the application's service functions directly, not through
`create_power_connection`'s full FastAPI route, so the *API-level*
error-translation code for Option A's retry loop does not exist yet to
test against — it is proposed, not built, in Part 6).

---

## Part 6 — Chosen Design: Option B (Global Advisory Lock)

**Selection: Option B**, for connection creation specifically.

1. **Why does it prevent F-C1?** By making every connection-creation
   transaction serialize against every other one at the very start
   (before either's cycle check runs), no transaction's cycle check can
   ever run concurrently with another's uncommitted insert — each cycle
   check is always evaluated against a fully-committed, fully-current
   graph. This is not a probabilistic mitigation; it is a structural
   guarantee (mutual exclusion), proven by Experiment 3 across 5 trials
   and by the underlying argument that a single global mutex admits no
   concurrent execution of the protected section at all.
2. **Correctness proof:** the protected section (lock → cycle check →
   insert → commit) executes under a system-wide mutual-exclusion lock;
   by definition of mutual exclusion, no two invocations of this section
   are ever concurrently active; therefore every cycle check observes a
   graph state that reflects every previously-committed connection
   creation and none that are still in flight; therefore the existing
   (already-correct-in-isolation) `assert_would_not_create_cycle` logic
   is always evaluated against ground truth, which is exactly the
   condition under which its own single-transaction correctness
   (uncontested by this audit) suffices for the whole system.
3. **PostgreSQL rejection:** none expected in the normal case — Option B
   does not introduce a new PostgreSQL-level failure mode; the existing
   canonical node-pair lock and cycle check behave exactly as before,
   just never concurrently with another instance of themselves.
4. **Deadlock:** not newly introduced (Part 4); existing deadlock
   handling, if any exists elsewhere in the codebase for `FOR UPDATE`
   contention, is unaffected.
5. **API response:** unchanged from today — a losing request still
   receives the existing 422 `WouldCreateCycle`/"Invalid Topology"
   response, now correctly reflecting the *post-serialization* graph
   state rather than a stale pre-collision one.
6. **Automatic retry required?** No — a losing request already got the
   *correct* answer (the edge really would create a cycle, given what is
   now actually committed), so there is nothing to retry. This is a
   material advantage over Option A.
7. **Retry count:** N/A.
8. **All retries fail:** N/A.
9. **Idempotency interaction:** clean — see Part 4's Option B analysis.
10. **Audit/outbox interaction:** unaffected — see Part 4.
11. **Performance impact:** full serialization of this one operation,
    system-wide; measured overhead in the 2-way case was ≈3ms uncontended,
    ≈77ms when directly contended against a 50ms-holding peer (Experiment
    3). Not measured under higher concurrency (Part 5's explicit
    limitation).
12. **Contention scope:** every `POST /power/connections` request,
    regardless of which nodes it touches, serializes against every other
    concurrently-in-flight one — broad by design, justified by the
    expected low frequency of this specific mutation in real usage.
13. **Behavior at 1,000 / 10,000 / 100,000 active nodes:** unaffected by
    node count — the lock's cost is independent of graph size (Part 4).
    Should the *rate* of concurrent connection-creation ever become high
    enough for this to be a measured bottleneck, that would be the
    trigger to revisit toward a narrower mechanism — not a reason to
    avoid this design now.
14. **Can direct DB writes bypass it?** Yes, exactly as they already
    bypass the existing canonical-order locking today (Part 4) — this is
    an accepted, pre-existing limitation of every application-level
    concurrency control in this codebase, not a new gap. If closing this
    specific bypass is ever required, it would need a `CONSTRAINT
    TRIGGER` performing the same traversal at the database level — a
    materially larger, separate design exercise, out of scope for this
    correction (flagged as `REQUIRES ARCHITECTURAL DECISION` in Part 14).
15. **What must future developers do?** Any new code path that inserts a
    `PowerConnection` row — a bulk-import endpoint, an admin tool, a data
    migration — **must** acquire the same fixed advisory-lock key before
    its own cycle check, via a shared helper (proposed name:
    `power_graph.with_connection_mutation_lock(db)`, a context-manager-
    style wrapper around `SELECT pg_advisory_xact_lock(...)`) rather than
    reimplementing the raw SQL inline. This should be documented
    prominently in `power_graph.py`'s own module docstring, exactly where
    the existing canonical-order-locking discipline is already documented,
    so it is discovered by the next person touching this file rather than
    only by someone who reads this design document.

---

## Part 7 — F-H1: Capacity-Exception Masking

**Intended operational principle (restated from the master prompt, now
made precise):** *any* capacity-related quantity a client-facing surface
(dashboard, `/power/capacity-exceptions`, a node's own capacity panel)
would need in order to correctly judge "is this healthy" must be either
computed and shown, or explicitly flagged as unknown/unavailable — never
silently omitted such that its absence reads as "nothing to report here."

**Exact behavior to define:**

- **Unknown allocation** (capacity known, allocation unknown — the exact
  F-H1 case): must produce a `CAPACITY_UNKNOWN` exception. This is the
  central correction.
- **Unknown utilization** (derived from the above — already correctly
  `None` today, just not surfaced as an exception): covered by the same
  fix, since `utilization_pct is None` is already the branch
  `derive_node_capacity_exceptions` enters; the fix is to stop requiring
  `effective_capacity_kw is None` as an additional condition within that
  branch.
- **Traversal bound exceeded:** already correctly propagates into
  `data_quality == "unknown"` via `compute_allocated_kw`'s existing
  `return None, "unknown"` on bound-exceeded — no change needed to that
  function itself, only to how its result is *consumed* by exception
  derivation.
- **Missing power path** (`POWER_PATH_MISSING`): unaffected by this fix —
  already a distinct condition, derived independently in
  `equipment_power_summary`/the redundancy-exception loop in
  `list_capacity_exceptions`, not from `derive_node_capacity_exceptions`.
  Left as-is.
- **Missing capacity** (no `PowerCapacity` record at all): already
  correctly produces `CAPACITY_UNKNOWN` today (`effective_capacity_kw is
  None` branch) — unaffected, this design only removes the *additional,
  wrong* precondition that excluded the allocation-unknown case.
- **Malformed/invalid capacity data:** out of scope for this fix — DB
  CHECK constraints already reject negative/out-of-range values at
  write time (confirmed in the hostile audit's Section 7); nothing
  malformed can reach this code path today.
- **Partially known graph / disconnected graph:** covered by the same
  general fix — any node whose `data_quality` resolves to `"unknown"` for
  *any* reason gets `CAPACITY_UNKNOWN`.
- **Cycle-prevention failure:** not this endpoint's concern — a graph that
  is cyclic due to F-C1 could in principle cause `compute_allocated_kw`'s
  own bounded traversal to behave unexpectedly (though its `visited` set
  and node-count bound should still terminate it safely, just
  potentially reporting a wrong number rather than crashing) — this is a
  downstream consequence of F-C1, not a separate defect; fixing F-C1
  removes the possibility of a cyclic graph rather than requiring
  `compute_allocated_kw` to defend against one directly. Recommend
  re-verifying this specific interaction once F-C1's fix is implemented,
  as a targeted regression test (Part 15).
- **Stale data:** not applicable — every capacity read in this system is a
  live query, not a cache (confirmed by the source-of-truth review in the
  hostile audit).

**Precise exception precedence rule (proposed):**

```
if figures.data_quality == "unknown":
    emit CAPACITY_UNKNOWN (severity="info")
    # and STOP — do not also attempt to evaluate overload/near-limit
    # thresholds against a utilization_pct that is None; there is nothing
    # meaningful to threshold-check yet.
elif figures.utilization_pct is None:
    # effective_capacity_kw is None (no rated/configured capacity at all) --
    # already covered by data_quality == "unknown" above in practice, since
    # get_capacity_figures already sets data_quality="unknown" whenever
    # effective is None (Section: existing code, unchanged) -- this branch
    # becomes unreachable once the fix above is in place, but is kept as
    # an explicit defensive case in the design rather than assumed away.
    emit CAPACITY_UNKNOWN (severity="info")
elif figures.utilization_pct >= critical_pct or figures.utilization_pct > 100.0:
    emit CAPACITY_OVERLOAD (severity="critical")
elif figures.utilization_pct >= warning_pct:
    emit CAPACITY_NEAR_LIMIT (severity="warning")
# else: healthy, no exception -- unchanged.
```

**Can multiple exceptions coexist for one node?** Under this precedence,
no — `CAPACITY_UNKNOWN` and `CAPACITY_OVERLOAD`/`CAPACITY_NEAR_LIMIT` are
mutually exclusive for a single node's *capacity* exceptions (you cannot
simultaneously not know the utilization and know it exceeds a threshold).
`REDUNDANCY_DEGRADED`/`POWER_PATH_MISSING` remain a *separate* exception
category (computed by a different function, over a different entity —
equipment, not power node) and can coexist with either capacity outcome
for the same equipment item, exactly as today.

**Dashboard / `/power/capacity-exceptions` / API / UI behavior:** no
response-shape change is required — `CAPACITY_UNKNOWN` is already a
defined, documented condition code in the API contract (per
`PHASE3_TRACEABILITY_MATRIX.md` row 17); this fix makes it reachable in a
new situation, it does not add a new code. The dashboard's existing
exception-list rendering already handles this code (it is exercised
today by the "no capacity record at all" case) — no frontend change is
anticipated, though the independent red-team should confirm this by
actually triggering the fixed condition through the UI once implemented
(not done in this design pass).

---

## Part 8 — F-H2: Retired PowerNode Semantics

**Do not assume the current behavior is either correct or wrong without
first choosing a model.**

**Model A — retired nodes excluded from active/operational traversal and
capacity, but preserved for history:** every traversal and roll-up query
gains an additional `PowerNode.retired_at IS NULL` filter (joined in,
since `_traverse`/`compute_allocated_kw` currently query
`PowerConnection` alone without joining `PowerNode` at all); a historical
view (audit log, a possible future "topology as of date X" feature) can
still reconstruct the pre-retirement graph from `PowerConnection`'s
`effective_from`/`effective_to` and `PowerNode.retired_at` together, since
neither the node nor its connection rows are ever deleted.

**Model B — retired nodes remain fully traversable until their
connections are explicitly removed:** current behavior. Requires the
*operator* to remember to also disconnect a node's connections as a
separate, explicit step when retiring it, or the "retirement" has no
practical effect on any computed value.

**Model C — separate historical vs. operational topology concepts:** a
larger modeling change (e.g., a distinct "as-built" vs. "as-operated"
graph, or an explicit `topology_snapshot` concept) — this is materially
more invasive than either A or B and is not justified by the scope of a
correction to an existing defect; it would be appropriate for a future
phase if a genuine historical-topology-reconstruction requirement
emerges, not as a reaction to this specific finding.

**Recommendation: Model A.** Reasoning:

- **Audit/history:** fully preserved either way — nothing in Model A
  deletes or hides historical rows, it only changes which rows an
  *operational* query considers. `audit_log` already has the full
  retirement event recorded regardless.
- **Capacity roll-up:** Model A is the only one of the three that makes
  "retire this PDU" have the operationally obvious effect (it stops
  counting toward its ancestors' allocated capacity) without requiring a
  second manual step. Model B's "current behavior" requires an operator
  to *also* remember to disconnect every one of a retired node's
  connections, and until they do, every calculation is silently wrong in
  exactly the way F-H2 found — Model A removes that manual-step
  dependency entirely.
- **Active topology / incident investigation:** Model A's "operational"
  view is what an incident responder actually wants ("what is *currently*
  feeding this rack") — a retired PDU that still shows up in a live
  upstream-path query is actively misleading during an incident, which is
  a correctness-adjacent operational risk, not merely a bookkeeping nicety.
- **Replacement workflow:** when a PDU is physically swapped, the
  intended sequence becomes: retire the old `PowerNode` (Model A now
  correctly removes it from active traversal/capacity immediately, no
  extra step) → create a new `PowerNode` for the replacement → create new
  `PowerConnection` rows wiring the replacement into the same position.
  The *old* connections are left as historical rows with whatever
  `effective_to` they already carry (or gain one at retirement time — see
  below) rather than needing manual disconnection first.
- **Equipment retirement:** the same reasoning applies symmetrically to
  `equipment_power_input` nodes — a retired equipment item's feed nodes
  should stop contributing to their upstream PDU's allocated-capacity
  count once retired, for the same "shouldn't need a second manual step"
  reason.
- **Power path validity:** a `POWER_PATH_MISSING` check that walks through
  a retired intermediate node today could report "path OK" through a node
  that is administratively retired — clearly wrong under Model A's
  intended semantics, correctly excluded once the filter is added.
- **Dashboard interpretation:** consistent with "operational" framing
  throughout the rest of the dashboard (it already only ever means
  "right now," never "ever, historically").
- **Future telemetry/alarm integration:** Model A is the cleaner
  foundation — a future alarm engine reading "this rack's power path" via
  the same traversal functions should never have to separately re-filter
  out retired nodes itself; that filter belongs once, at the traversal
  layer, not re-derived by every future consumer.

**Explicit per-entity-type behavior under Model A:**

- **Retired source/intermediate/PDU/UPS/generator/panel node:** excluded
  from `_traverse`'s BFS frontier expansion entirely (a retired node's
  outgoing/incoming active connections are simply never followed) —
  equivalent to treating it as if it had no connections, without actually
  touching the connection rows.
- **Retired outlet:** same treatment — a `pdu_outlet`-type `PowerNode`
  with `retired_at` set is excluded the same as any other retired node.
- **Connections involving a retired node:** left as `effective_to IS
  NULL` (untouched) at retirement time under this design — retirement
  filters the *node* out of traversal, it does not need to also close the
  *connection* rows, since the traversal-level exclusion already achieves
  the correct operational result. (An alternative, stronger design would
  auto-close all of a node's active connections at the moment of
  retirement — rejected here as unnecessary complexity: it would require
  retirement to become a multi-row transactional operation with its own
  audit/outbox implications, for no additional correctness benefit beyond
  what the traversal-level filter already provides. Flagged as a
  `REQUIRES ARCHITECTURAL DECISION` alternative in Part 14 in case a
  future reviewer disagrees.)
- **Downstream equipment of a retired intermediate node:** correctly
  reported as `POWER_PATH_MISSING` under Model A (the retired node is no
  longer a valid hop), which is the operationally correct signal — "this
  equipment's power path currently runs through nothing" is exactly true
  once the intermediate is retired and nothing has been rewired yet.

---

## Part 9 — F-M1: If-Match Correction

**Existing shared helper, confirmed correct:**
`app.application.concurrency.parse_if_match`/`require_if_match`
(`backend/app/application/concurrency.py` lines 12-36) — already handles
missing (`None`, returns `None`, caller decides), valid integer,
quoted-integer (`strip('"')`), and malformed (`ValueError` → `ApiError(400,
"Bad Request")`) correctly. `PowerConnection` PATCH already uses this via
`Depends(require_if_match)`.

**Correction:** replace `set_node_capacity`'s ad hoc `if_match: str | None
= Header(...)` parameter plus its inline `int(if_match.strip().strip('"'))`
call with the same `parse_if_match` function (not `require_if_match`,
since capacity PUT's precondition is *conditionally* required — only when
a current record already exists — unlike connection PATCH where it is
always required; `parse_if_match` alone, called explicitly, preserves that
conditional-requirement branching exactly as `set_node_capacity` already
structures it, just replacing the unsafe `int(...)` call with the
already-hardened parser).

**Expected behavior, explicitly, post-correction:**

| Input | Behavior |
|---|---|
| Missing `If-Match`, no current record | Proceeds (first-time creation, unchanged from today) |
| Missing `If-Match`, current record exists | `428 Precondition Required` (unchanged from today) |
| Valid integer value, matches current version | Proceeds (unchanged from today) |
| Valid integer value, stale (mismatched) version | `409 Conflict` via `check_version_match` (unchanged from today) |
| Quoted value (`"3"`) | Parsed correctly via `.strip('"')` (unchanged behavior, now via the shared helper) |
| Malformed (non-integer) value | **Corrected: clean `400 Bad Request`** (today: unhandled `ValueError` → generic `500`) |
| Negative integer version (e.g. `-1`) | Parses successfully as an integer (no `ValueError`), then fails `check_version_match` as a simple mismatch → `409 Conflict` — this is correct and requires no special-casing, since no real version is ever negative, so a negative `If-Match` can never accidentally match |
| Zero | Same as negative — parses fine, then almost certainly fails the version-match check (`409`), since `version` starts at 1 — correct, no special-casing needed |
| Duplicate `If-Match` headers | Not evaluated in this design pass — **NOT EXECUTED**; this is a Starlette/FastAPI header-parsing behavior question (does `Header(default=None)` even accept multiple values, or does the ASGI layer already collapse/reject duplicates before this code runs), out of scope for this specific correction since it is not one of the hostile audit's confirmed findings — flagged for the red-team to check independently if desired. |

---

## Part 10 — F-M2: Redundancy Classification Reassessment

**Correctness vs. operational-usefulness distinction, made explicit:** the
hostile audit already established (and this design reaffirms by re-reading
`equipment_power_summary` again) that the shared-ancestor detection is
*correct* — it finds real single points of failure, with no false negative
found in either the original audit or this re-review. The concern is
purely **operational semantics / alarm fatigue / presentation**, not a
logic defect.

**Assessment: this is a presentation/documentation concern, not a
correction-scope defect.** Recommendation: **do not invent a new
redundancy model.** The existing `dual_feed_healthy`/`degraded`/
`single_feed`/`no_power_modeled` four-way classification is the right
*shape*; what is missing is a way to distinguish *why* a pair is
`degraded` (shared ancestor vs. missing upstream vs. duplicate feed label)
in the API response, so a dashboard or operator can tell "this is
expected, because our site genuinely has one utility feed" from "this is
a real, newly-introduced SPOF." This is additive (a new field on the
existing response, e.g. `degraded_reason: "shared_ancestor" |
"missing_upstream" | "ambiguous_labeling"`), not a redesign of the
classification logic itself, and can reasonably be deferred to a future
phase's dashboard-UX work rather than bundled into this correction, since
it does not change any computed value's correctness — only its
explanatory detail. **Classification: `DOCUMENTATION ONLY` for this
correction pass** (document the expected-alarm-volume characteristic in
the module docstring so a future maintainer isn't surprised by it),
**`REQUIRES ARCHITECTURAL DECISION`** for whether/when the `degraded_reason`
field is worth adding.

---

## Part 11 — F-M3: Subtype Integrity

**Is API validation sufficient for Phase 3, or is DB-level enforcement
required now?**

**Recommendation: DB-level enforcement is achievable without triggers,
using PostgreSQL's standard "typed foreign key" pattern, and should be
part of this correction** given the master prompt's own explicit
instruction that "database constraints should enforce what PostgreSQL can
enforce reliably" — this case qualifies, because `ManagedAsset` already
carries an `asset_type` column (confirmed: `_create_power_asset` in
`power.py` sets `asset_type="pdu"`/`"ups"`/etc. on every row it creates).

**Mechanism (design only, not implemented):**

1. Add a `UNIQUE (id, asset_type)` constraint on `managed_asset` (harmless
   in addition to its existing primary key on `id` alone — a composite
   unique constraint over a column that is already unique plus one more
   column is always satisfiable and adds no new rejection behavior to
   `managed_asset` itself).
2. On `pdu_outlet`, add a generated column,
   `pdu_asset_expected_type` `TEXT GENERATED ALWAYS AS ('pdu') STORED`,
   and change the existing plain FK on `pdu_asset_id` to a composite FK:
   `FOREIGN KEY (pdu_asset_id, pdu_asset_expected_type) REFERENCES
   managed_asset (id, asset_type)`.
3. The same pattern applies to `power_node.owning_asset_id` for
   `equipment_power_input`-typed rows referencing `Equipment` — though
   this is structurally harder, since a single `owning_asset_id` column
   on `power_node` is shared across three different `node_type` values
   (`power_circuit`, `pdu_outlet`, `equipment_power_input`) each expecting
   a *different* target subtype, so a single fixed-literal generated
   column does not work here the way it does for `pdu_outlet` (whose
   `pdu_asset_id` always means exactly one subtype, `pdu`). This would
   require either three separate nullable composite-FK columns (one per
   possible subtype, each fixed to its own literal, always exactly one
   non-null — structurally similar to `PowerNode`'s own existing
   `managed_asset_id`/`owning_asset_id` exclusivity pattern) or a
   `CONSTRAINT TRIGGER` performing the correct-subtype check based on
   `node_type`'s runtime value. **This half of F-M3 is materially harder
   than the `pdu_outlet` case and is not fully worked out at the
   constraint level in this design pass.**

**Classification:**

- `pdu_outlet.pdu_asset_id` subtype integrity: **`FIX IN CORRECTION`** —
  the composite-FK-with-generated-column pattern is straightforward,
  requires one migration, and directly closes a concrete, named gap.
- `power_node.owning_asset_id` subtype integrity (for
  `equipment_power_input`/`power_circuit`): **`REQUIRES ARCHITECTURAL
  DECISION`** — the mechanism above does not cleanly generalize, and
  forcing it through a `CONSTRAINT TRIGGER` is a larger, separate design
  question (procedural constraints in the schema, a departure from this
  codebase's otherwise pure declarative-constraint style) that deserves
  its own review rather than being folded into this correction silently.

---

## Part 12 — F-M5: Observability

**Structured logging fields to add** (all via the existing `get_logger`/
structlog-style pattern already used in `core/errors.py`, extended into
`power_graph.py`/`power_capacity.py`, which today have zero logging calls
of their own):

| Event | Level | Fields |
|---|---|---|
| Traversal bound exceeded (`GraphTraversalBounded`) | `warning` | `root_id`, `limit_kind` ("depth"/"nodes"), `direction` ("upstream"/"downstream"), `request_id`, `correlation_id` |
| Cycle rejected (`WouldCreateCycle`) | `info` | `source_node_id`, `target_node_id`, `request_id`, `correlation_id` — `info`, not `warning`, because a rejected cycle attempt is the system working correctly, not a fault |
| Serialization conflict, if Option A were chosen (N/A under the chosen Option B — retained here for completeness in case a future reviewer picks Option A instead) | `warning` | `sqlstate`, `retry_attempt`, `source_node_id`, `target_node_id` |
| Advisory-lock wait exceeding a notable threshold (e.g. >500ms) | `info` | `wait_ms`, `lock_key` — a leading indicator of contention worth watching before it becomes a real bottleneck |
| Capacity-exception derivation hitting `CAPACITY_UNKNOWN` due to bounded allocation (the F-H1 fix's new path, distinct from the pre-existing "no capacity record at all" path) | `info` | `power_node_id`, `alloc_quality` |
| DB CHECK-constraint violation on any Phase 3 table (already caught generically by `core/errors.py`'s `IntegrityError` handler — recommend adding the specific constraint name to that existing log line, which today logs only `str(exc.orig)`, already containing it, so this may already be sufficient — **verify, not necessarily change**) | `warning` (existing) | Already logs `str(exc.orig)`, which PostgreSQL formats to include the constraint name — confirm this is legible enough as-is before adding a redundant explicit field. |

No secrets, credentials, or full request bodies should appear in any of
these — all fields above are IDs, enums, and counts, consistent with the
rest of the codebase's existing logging discipline.

---

## Part 13 — Phase 1 NEW-1 Reassessment

**Does NEW-1 remain valid?** Yes — nothing in Phase 3 touches
`idempotency.py`, `release_claim`, or `complete_claim`; the narrow race
described in `PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md` §7 (an original
claim-holder's write failing with an unhandled `IntegrityError` after
being superseded by a stale-timeout reclaimer, propagating a spurious 409
instead of replaying the reclaimer's successful response) is unchanged by
this commit.

**Does Phase 3 create additional exposure?** No new endpoint in Phase 3
uses `Idempotency-Key` handling at all (confirmed by grep, Part 1) — NEW-1
is a property of the *existing* idempotency mechanism itself, and Phase 3
does not add a new caller of `get_or_claim`/`complete_claim` that could
newly trigger it.

**Does the F-C1 fix (Option B) interact with NEW-1?** No — Option B adds
an advisory lock and, if a future idempotency-key-bearing version of
connection-creation is ever built, the "which failure paths need
`release_claim`" question is unchanged by Option B's presence (a lock
acquisition or cycle-check failure under Option B is just one more kind
of "the claimed write failed, release the claim" case, structurally
identical to any other failure the endpoint could already raise today).
Option A (not chosen) would have introduced a *new* interaction requiring
explicit design (Part 4's Option A idempotency note) — this is one more
point in favor of Option B being the simpler, lower-risk choice.

**Must NEW-1 be corrected before Phase 3 approval?** No — it is a Phase 1
carry-forward, narrow-trigger (requires a 30+ second stall plus a
concurrent duplicate), no-data-corruption defect, already assessed as
non-blocking by the Phase 1 gate itself, and Phase 3 neither worsens nor
depends on it. **Classification: `ACCEPTED CARRY-FORWARD`, unchanged.**

**Recommended correction, for the record (not part of this correction's
required scope):** exactly as the Phase 1 report's own "Recommended
remediation direction" already states — before treating a write failure
as fatal, re-check whether the claim row has moved to `'completed'` under
a different claimant (a fencing-token/generation check) and replay that
response instead of propagating the error. This remains well-scoped and
consistent with the existing design; it simply has not been prioritized
above the Phase 3-specific findings in this correction pass.

---

## Part 14 — Every Other Hostile-Audit Finding, Classified

| Finding | Severity | Status (this design pass) | Action | Reason |
|---|---|---|---|---|
| F-C1 cycle race | CRITICAL | Confirmed, re-verified (Exp. 1) | **FIX IN CORRECTION** (Option B) | Core correctness invariant of the whole phase |
| F-H1 capacity masking | HIGH | Confirmed | **FIX IN CORRECTION** (Part 7) | Directly contradicts "never silently healthy" |
| F-H2 retirement semantics | HIGH | Confirmed; semantics now made explicit (Model A) | **FIX IN CORRECTION** (Part 8) | Ambiguous-intent finding resolved by this design; implementation still required |
| F-M1 malformed If-Match | MEDIUM | Confirmed | **FIX IN CORRECTION** (Part 9) | Small, well-scoped, shared-helper reuse |
| F-M2 redundancy alarm volume | MEDIUM | Reassessed: presentation, not correctness | **DOCUMENTATION ONLY** now; `REQUIRES ARCHITECTURAL DECISION` for the optional `degraded_reason` field | Classification correct, explanatory detail is additive future work |
| F-M3 subtype integrity (`pdu_outlet`) | MEDIUM | Confirmed, mechanism identified | **FIX IN CORRECTION** (Part 11) | Achievable without triggers, closes a real gap |
| F-M3 subtype integrity (`owning_asset_id`) | MEDIUM | Confirmed, mechanism not fully solved | **REQUIRES ARCHITECTURAL DECISION** | Composite-FK trick doesn't generalize cleanly; needs its own review |
| F-M4 no site/org scoping on `/power/*` | MEDIUM | Reaffirmed, by-design per master prompt | **ACCEPTED CARRY-FORWARD** | Explicit master-prompt scope decision, not an oversight; real risk only if deployed multi-tenant |
| F-M5 no structured logging | MEDIUM | Confirmed | **FIX IN CORRECTION** (Part 12) | Directly requested by master prompt §30, low implementation cost |
| F-L1 `redundancy_factor` unused | LOW | Confirmed | **DOCUMENTATION ONLY** | Decorative field; wiring it in is a modeling decision, not a bug fix — note in module docstring that it is operator-facing metadata only, pending Part 10's broader redundancy-detail decision |
| F-L2 `POWER_TOPOLOGY_INVALID` unreachable | LOW | Confirmed | **FIX IN CORRECTION** (opportunistic) | Part 7's fix could reuse this exact code for the "graph itself is invalid" sub-case if the red-team later confirms F-C1's traversal-bound interaction (Part 7's cycle-prevention-failure note) needs its own distinct signal — otherwise leave defined-but-rare, which is acceptable | 
| F-L3 CASCADE/RESTRICT FK landmine | LOW | Confirmed, currently unreachable | **ACCEPTED CARRY-FORWARD** | No hard-delete endpoint exists anywhere in Phase 1/2/3 today; revisit only if one is ever added |
| Dashboard N+1 | INFO (perf) | Not re-measured this pass | **OUT OF SCOPE** for this correction | Performance work, not a correctness defect; needs its own measurement-first pass per master prompt's "measure before optimizing" |
| Full-scale performance untested | INFO | Not re-measured this pass | **OUT OF SCOPE** for this correction | Same reasoning; flagged for red-team, not a code change |
| Migration re-validation | INFO | Not re-executed this pass | **OUT OF SCOPE** for this correction | No schema change proposed here except Part 11's `pdu_outlet` addition, which would need its own fresh/up/down/re-up validation once implemented |
| Audit-log field-content verification | INFO | Not re-executed this pass | **OUT OF SCOPE** for this correction | Independent verification task, not a code change |
| NaN/Infinity/extreme capacity values | INFO | Not tested this pass | **REQUIRES ARCHITECTURAL DECISION** (is Pydantic's default float handling sufficient, or does an explicit `finite=True` validator need adding?) | Genuinely unknown without testing; not assumed either way |
| 3+-transaction cycle race | INFO | Not tested this pass (only 2-way) | **OUT OF SCOPE** for this correction, but the chosen fix (Option B, global lock) analytically extends to N-way trivially (a single mutex serializes any number of contenders, not just two) — **ANALYTICAL CONCLUSION**, not executed | 
| Multi-tenant data leakage | INFO | Not tested this pass | **ACCEPTED CARRY-FORWARD**, same as F-M4 | Same underlying gap, same reasoning |
| `POWER_TOPOLOGY_INVALID` reuse for F-C1's residual traversal-under-cycle question | — | See F-L2 row above | — | — |

No finding was downgraded in severity from the hostile audit without the
explicit reasoning shown above (F-M2's reassessment from "correctness
concern" framing to "presentation concern" is the only reclassification
in this table, and it is the *same* conclusion the hostile audit itself
already reached in its own Section 30 item — not a new downgrade
introduced here).

---

## Part 15 — Regression Test Design (To Be Implemented in Correction Phase)

**Concurrency:**

- Exact F-C1 race (the 4-node/2-pre-existing-edge scenario), reproduced
  as a proper `pytest` test using the `per_request_client` pattern already
  established in `test_idempotency_concurrency.py`/`test_power.py`'s own
  concurrent-connection test — asserting the fix (Option B) results in
  exactly one commit and one clean `WouldCreateCycle`-derived 422, never
  both committing.
- Repeated iterations of the same race (≥10 trials in one test run) to
  catch any timing-dependent flakiness the single-shot version might miss.
- 3+ concurrent writers extending the race to a longer cycle (N nodes, N
  concurrent transactions each adding one edge) — proves the fix
  generalizes beyond the minimal 2-transaction case analytically argued
  in Part 14.
- Overlapping endpoint locks (the case already correctly handled today —
  regression-guard it explicitly so a future change to the locking
  mechanism cannot silently reintroduce the *original*, simpler race
  while fixing the new one).
- Deadlock handling: a targeted test attempting to provoke a genuine
  Postgres deadlock (`40P01`) via some other, unrelated concurrent lock
  acquisition, confirming the advisory lock does not introduce one.
- Retry correctness: N/A under Option B (no retries needed) — if a future
  reviewer instead chooses Option A, this line item becomes mandatory
  (retry produces the eventually-correct final state, never a duplicate
  edge from a retried insert).
- Final-graph-acyclicity assertion: a general-purpose test helper that,
  given any sequence of concurrent mutations, asserts the resulting graph
  contains no cycle (reusable across all the above scenarios rather than
  hand-checking each one).

**Capacity:**

- Known allocation (existing coverage, unchanged).
- Unknown allocation **now correctly producing `CAPACITY_UNKNOWN`** — the
  exact regression test for F-H1, using the same monkeypatched
  `MAX_TRAVERSAL_NODES` technique this design's own diagnostic (and the
  original hostile audit) used.
- Traversal bound reached exactly at the boundary (off-by-one case: bound
  = N, subtree size = N vs. N+1).
- Overload/near-limit/healthy (existing coverage, unchanged).
- Missing capacity (existing coverage, unchanged).
- Negative available capacity preserved, not clamped (existing coverage,
  unchanged).
- A/B de-duplication and shared-upstream detection (existing coverage,
  unchanged — Part 10 concluded no logic change needed here).

**Retirement:**

- Retired source node excluded from downstream traversal starting
  elsewhere.
- Retired intermediate node excluded, correctly producing
  `POWER_PATH_MISSING` for equipment downstream of it.
- Retired PDU's capacity excluded from ancestor allocated-capacity
  roll-up.
- Retired downstream equipment's feed nodes excluded from *their*
  upstream PDU's allocated-capacity count.
- New-connection rejection for a retired node (existing coverage,
  unchanged — this already works correctly today).
- Active capacity semantics: confirm a *non-retired* node's own capacity
  figures are unaffected by an unrelated retirement elsewhere in the
  graph (a pure regression guard against an overly broad filter
  implementation).

**Concurrency headers:**

- Valid `If-Match` (existing coverage, unchanged).
- Malformed `If-Match` on capacity PUT, **now correctly producing `400`**
  — the exact regression test for F-M1.
- Missing `If-Match` (existing coverage for both endpoints, unchanged).
- Stale `If-Match` (existing coverage, unchanged).
- Quoted `If-Match` value (existing coverage via the shared helper,
  unchanged).

**Security:**

- Authorization (existing coverage, unchanged — no new endpoint
  introduced by this correction).
- IDOR (existing coverage, unchanged).
- Direct DB invariant bypass for the new `pdu_outlet` subtype constraint
  (Part 11) — a new DB-constraint test in the style of
  `test_phase3_power_constraints.py`, attempting to insert a `pdu_outlet`
  row referencing a non-`pdu` `ManagedAsset` id directly via SQL, and
  confirming the new composite FK rejects it.

**Idempotency:**

- Concurrent identical request: N/A (no idempotency-key-bearing endpoint
  touched by this correction) — explicitly noted as not applicable rather
  than silently skipped.
- Stale reclaim (NEW-1's existing test coverage — unaffected, unchanged).
- Transaction retry interaction: N/A under the chosen Option B (Part 13).

Tests above are designed to prove the stated invariants (acyclicity,
visibility of unknown data quality, correct retirement exclusion, clean
4xx on malformed input, subtype referential integrity) — not merely to
raise a coverage percentage.

---

## Part 16 — Acceptance Criteria

- No committed active `PowerConnection` graph cycle is possible under any
  tested concurrent mutation scenario, including the exact F-C1 race and
  its repeated/N-way extensions.
- A node with known capacity but unknown allocation always produces a
  visible `CAPACITY_UNKNOWN` exception; no node's real overload can be
  masked as "no exceptions."
- Retirement semantics follow Model A (Part 8), are documented in the
  domain model's own docstrings (not only in this design document), and
  are covered by the Part 15 test matrix.
- Malformed `If-Match` on capacity PUT returns the canonical `400`, never
  a `500`.
- No automatic-retry semantics are required under the chosen design
  (Option B); if a future reviewer instead selects Option A, retry
  semantics must be deterministic (bounded count, clean terminal `503` on
  exhaustion) before that alternative could be accepted.
- Idempotency (NEW-1 and otherwise) remains exactly as correct/incorrect
  as it already is today — this correction neither improves nor worsens
  it, and does not silently claim to have fixed it.
- Audit/outbox behavior is verified unaffected (same transaction
  boundaries as today, confirmed by code reading in Part 1; not
  independently re-executed against a live `audit_log` table in this
  design pass — that remains the red-team's task, per the hostile audit's
  own Section 31).
- The new `pdu_outlet` subtype-integrity constraint is validated by a
  direct-SQL-bypass test proving the database itself rejects the invalid
  reference, not merely that the API already prevented it.
- Every classification in Part 14's table is either implemented
  (`FIX IN CORRECTION` rows), explicitly and specifically documented in
  the code it concerns (`DOCUMENTATION ONLY` rows), or explicitly flagged
  for a human architectural decision before any further work proceeds on
  it (`REQUIRES ARCHITECTURAL DECISION` rows) — none are silently dropped.

---

## Part 17 — Implementation Sequence

1. **Advisory-lock primitive** (`power_graph.py`): add the named lock-key
   constant and a `with_connection_mutation_lock(db)` helper.
   *Files:* `backend/app/application/power_graph.py`.
   *Dependency:* none — this is the foundation everything else in F-C1's
   fix builds on.
   *Risk:* low — additive, no existing behavior changed by adding the
   helper itself (only by *calling* it, next step).
   *Tests required:* the helper's own acquire/release behavior under a
   single transaction (unit-level).
   *Rollback:* trivial — unused code, safe to leave or remove.

2. **Wire the lock into `create_power_connection`**
   (`backend/app/api/v1/power.py`): acquire the lock as the very first
   statement in the transaction, before `lock_node_pair_in_canonical_order`.
   *Dependency:* step 1.
   *Risk:* medium — changes the actual concurrency behavior of a
   production code path; must be tested under real concurrency, not just
   unit-level, before merging.
   *Tests required:* Part 15's full concurrency matrix.
   *Rollback:* revert to the pre-lock behavior is a one-line removal if a
   regression is found — no schema or data changes involved, so rollback
   is unusually cheap for this step.

3. **F-H1 fix** (`power_capacity.py`'s `derive_node_capacity_exceptions`):
   restructure the precedence logic per Part 7.
   *Dependency:* none (independent of steps 1-2).
   *Risk:* low-medium — changes exception-derivation output for a
   previously-silent case; must confirm no existing test asserts the
   *old* (silently-empty) behavior as if it were correct, or that test
   itself needs updating alongside this fix.
   *Tests required:* Part 15's capacity matrix, specifically the new
   "unknown allocation, known capacity" case.
   *Rollback:* straightforward, pure logic change, no schema.

4. **F-H2 fix** (retirement exclusion): add `PowerNode.retired_at IS
   NULL` filtering to `_traverse` (`power_graph.py`) and to
   `compute_allocated_kw`'s pass-1/pass-2 queries (`power_capacity.py`) —
   requires joining `PowerNode` into queries that today select only from
   `PowerConnection`.
   *Dependency:* none (independent of steps 1-3), but should land *after*
   step 2 so its own regression tests run against the corrected
   concurrency behavior, not the pre-fix race-prone one.
   *Risk:* medium — touches the hot-path traversal/allocation queries;
   must confirm the added join does not meaningfully regress the
   traversal performance characteristics already measured (loosely) in
   the original implementation's performance pass.
   *Tests required:* Part 15's full retirement matrix.
   *Rollback:* straightforward, pure query change, no schema.

5. **F-M1 fix** (`set_node_capacity`'s `If-Match` handling): replace the
   ad hoc parsing with `parse_if_match`.
   *Dependency:* none.
   *Risk:* low — small, isolated, reuses already-battle-tested code.
   *Tests required:* Part 15's `If-Match` matrix.
   *Rollback:* trivial.

6. **F-M5 observability**: add the structured log lines from Part 12.
   *Dependency:* steps 2-4 (log the *corrected* behavior's own new
   branches, e.g. the advisory-lock-wait log point only exists once step
   2 lands).
   *Risk:* low — logging is additive and cannot change functional
   behavior (verify no log call is placed where it could throw and mask
   the real exception it's meant to describe).
   *Tests required:* none strictly required for correctness, but a smoke
   test confirming each log line fires under its intended trigger
   condition is good practice.
   *Rollback:* trivial.

7. **F-M3 fix** (`pdu_outlet` composite FK): requires a new migration
   (`0007` or similar).
   *Dependency:* none functionally, but sequenced last among the schema-
   touching items since it is the only one requiring an actual migration,
   and migrations are the highest-risk category of change here (per the
   master prompt's own "no destructive migrations without justification"
   instruction, even though this one is purely additive).
   *Risk:* medium — any migration requires fresh/upgrade/downgrade/
   re-upgrade validation before being considered safe, per this
   codebase's own established discipline (migrations 0002-0006 all
   received this treatment).
   *Tests required:* Part 15's direct-SQL-bypass test, plus full
   migration validation.
   *Rollback:* the migration's own `downgrade()` must correctly drop the
   new generated column and composite FK without affecting existing
   `pdu_outlet` rows' other data.

8. **Full regression suite** (all 269 existing tests plus every new test
   from steps 2-7) — must pass before any of this is considered complete.

9. **Performance re-check** (advisory-lock contention specifically, plus
   a re-confirmation that the F-H2 join doesn't regress traversal timing)
   — targeted, not the full master-prompt-scale benchmark (still out of
   scope per Part 14).

10. **Migration validation** (fresh/upgrade/downgrade/re-upgrade for the
    new migration from step 7).

11. **Hostile re-audit**: re-run this same adversarial process (or an
    independent one) against the corrected implementation, specifically
    re-attempting the exact F-C1/F-H1/F-H2/F-M1 reproductions from
    `PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md`, to confirm each is actually
    closed rather than merely addressed on paper.

This order front-loads the highest-severity, most time-pressured fix
(F-C1, steps 1-2) and defers the one change requiring a database
migration (F-M3, step 7) to last among the implementation steps, since a
migration is the most expensive category of change to redo if an earlier
step's testing surfaces something that changes the plan.

---

## Part 18 — Final Design Decision

### CORRECTION DESIGN DECISION

| Finding | Decision | Implementation Required? | Phase 3 Gate Impact |
|---|---|---|---|
| F-C1 | Global advisory lock (Option B) around connection creation | **Yes** | Blocking — this is the core correctness property of the entire power-topology feature |
| F-H1 | Restructure exception precedence to key off `data_quality`, not `effective_capacity_kw` alone | **Yes** | Blocking — directly contradicts an explicit master-prompt invariant |
| F-H2 | Adopt Model A (operational exclusion of retired nodes from traversal/allocation) | **Yes** | Blocking — current behavior makes "retire" operationally meaningless without a manual follow-up step |
| F-M1 | Reuse existing `parse_if_match` helper | **Yes** | Non-blocking for the gate in isolation, but cheap enough to bundle with the rest |
| F-M2 | No logic change; document alarm-volume characteristic; defer `degraded_reason` field | No (documentation only) | Non-blocking |
| F-M3 | `pdu_outlet` composite FK: yes. `owning_asset_id` general case: architectural decision needed first | Partial — **Yes** for `pdu_outlet`, **deferred** for the general case | Non-blocking, but the `pdu_outlet` half is cheap and worth including |
| F-M5 | Add the six structured log points from Part 12 | **Yes** | Non-blocking, but directly requested by the master prompt |
| NEW-1 | No change — reaffirmed accepted carry-forward | **No** | Non-blocking, unaffected by this correction |

### Required Correction Scope

F-C1 (Option B advisory lock), F-H1 (exception precedence), F-H2
(Model A retirement filtering), F-M1 (shared `If-Match` helper), F-M5
(structured logging), and F-M3's `pdu_outlet` half (composite FK
migration) — six implementation items, sequenced per Part 17.

### Deferred / Carry-Forward Scope

F-M2's `degraded_reason` field (future dashboard-UX work); F-M3's general
`owning_asset_id` subtype constraint (needs its own architectural
decision on the trigger-vs-multi-column-FK tradeoff); F-L1
(`redundancy_factor`, documentation only); F-L3 (CASCADE/RESTRICT
landmine, unreachable today); dashboard N+1; full-scale performance;
migration re-validation beyond the new F-M3 migration; audit-log
field-content verification; NaN/Infinity input handling; 3+-transaction
race testing (analytically covered by Option B's generalization, not
independently executed); multi-tenant scoping (F-M4, accepted by design);
Phase 1 NEW-1 (accepted carry-forward, unchanged).

### Risks Remaining After Correction

- Option B's advisory lock has not been throughput-tested beyond 2-way
  contention; if connection-creation frequency in real usage turns out
  higher than expected, this could become a measured bottleneck requiring
  a future, narrower locking scheme (Option C's rejected approach, or a
  genuinely different one, would need re-evaluation at that point, not
  before).
- A raw SQL bypass of the advisory lock (or of the new `pdu_outlet`
  composite FK's API-layer-only counterpart for the unresolved
  `owning_asset_id` case) remains possible for anyone with direct database
  access — an accepted, pre-existing characteristic of this codebase's
  overall concurrency-control philosophy, not newly introduced by this
  design.
- The 3+-transaction cycle-race generalization of Option B is an
  analytical conclusion, not an executed proof — the implementation
  sequence's step 8 test matrix should close this gap before the next
  hostile re-audit, but it is not yet closed as of this design document.
- F-H2's Model A choice is this design's own recommendation, not yet
  validated against real operator expectations — the independent red-team
  or a product-side reviewer should confirm this is genuinely the
  intended domain behavior before implementation, not merely internally
  consistent.

### Conditions Required Before Independent Red-Team

1. All six `FIX IN CORRECTION` items from the table above implemented
   per Part 17's sequence.
2. Full Part 15 regression test matrix passing.
3. Full existing 269-test suite still passing (zero regressions).
4. New migration (F-M3's `pdu_outlet` half) validated
   fresh/upgrade/downgrade/re-upgrade.
5. A hostile re-audit (Part 17 step 11) specifically re-attempting the
   F-C1/F-H1/F-H2/F-M1 reproductions, confirming each is closed.
6. This design document itself reviewed and approved by a human before
   any of the above implementation work begins, per the stop condition
   this task was given.

No implementation has occurred. No commit has been made for this design.
