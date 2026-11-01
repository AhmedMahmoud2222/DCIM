# Controller Reliability Specification, Document A: Journal, Delivery and Rollback Trust

Status: **DRAFT specification. Not reviewed. Not accepted.** No implementation, test or CI result is asserted by this document.

- Authored by: Claude (patch delivery, DX12). Applied by: the orchestrator.
- Reference baseline: `main@d0faef8dafa41b0ef4b7668f518efc0bd2d49786`.
- Subject: controller reliability work in PR #219 (frozen). Source material: parked PR #228 and issue #220. This document does not copy that material verbatim and does not inherit its contradictions.
- Authority: owner decision D5 "specification first" and A7 split execution (issue #220). The partition plan is a drafting approach only and not technical acceptance of any invariant.
- Production execution, controller activation and any POST-issuing mode are **not** authorized by this document (see section 8).

The keywords MUST, MUST NOT and SHOULD are used in the RFC 2119 sense. Names such as "journal" or "Store" denote roles, not code identifiers.

## 1. Scope, glossary and state enum

This document is the single authoritative source for the invariants, transitions and decisions listed in the ownership table (section 9). It contains no reference to any other specification document. Where behaviour depends on policy owned elsewhere, section 7 states a parameter contract: a named input that a consumer document or implementation MUST supply.

| Term | Meaning |
| --- | --- |
| Journal | The single signed, versioned document that is the controller's only durable state. |
| Task | One unit of orchestration work, identified by a stable `task_id`. |
| Store | The remote object holding the journal. It supports read (content plus version token) and conditional write (PUT with expected version token). |
| Version token | The Store's opaque revision identifier. |
| Checkpoint | The advance-only `(generation, journal_digest)` pair held in the trust anchor (INV-S4). |
| Trust anchor | The medium holding the checkpoint, outside the Store's writable path. Its choice is open (D-8). |
| Delivery | One non-idempotent POST to an external system, identified by `(task_id, delivery_id)`. |
| Confirmed journal | The newest journal the run has confirmed by a definite write success or by a successful INV-A2 classification (INV-A5). |
| Authorizing write | A journal write whose durable effect permits an external side effect (T3 `reserved`). |
| Outcome write | A journal write that records the result of an external side effect (T4). It never authorizes that effect. |
| Mutation | A function of the current journal that yields a new journal, carrying a unique `mutation_id`. |

Task states (enum): `ingested`, `pending`, `reserved`, `delivered_unconfirmed`, `confirmed` (terminal), `escalated` (terminal for automation), `blocked` (terminal). This document defines the meaning of `reserved` and `delivered_unconfirmed` and the immutability of terminal states. It does not define how tasks reach `ingested`, `pending` or a terminal state other than through the contracts in section 7.

## 2. Invariants

### 2.1 Signing and rollback trust (INV-S)
- **INV-S1** Every journal write carries a signature over the canonical serialization, bound to the journal schema version and a monotonic `generation` counter. The signature covers all task records.
- **INV-S2** A journal that fails signature, schema or generation validation MUST be rejected whole. The controller MUST NOT act on partially valid content and MUST NOT overwrite it (fail closed, alert a human).
- **INV-S3** `generation` MUST increase by exactly one per successful write. A read returning a lower generation than one already observed in this run MUST be treated as stale (INV-C3).
- **INV-S4** (cross-run non-rewindable history) Every journal read MUST be verified against an independently trusted monotonic checkpoint `(generation, journal_digest)` held in a medium that the Store write credential cannot rewrite. Each journal records `prev_digest`, the digest of the journal at `generation - 1`. A journal is refused before any PUT, any POST and any mutation if its `generation` is lower than the checkpoint's, or equal with a different digest, or its `prev_digest` chain does not reach the checkpoint. This holds across processes and restarts; run-local memory (INV-S3) is not sufficient.
- **INV-S5** (checkpoint initialization, advance and recovery) The checkpoint is advance-only and is advanced only after a journal write is confirmed (INV-A5). A checkpoint lagging the journal by exactly one generation, with a matching `prev_digest`, is accepted and advanced (crash between journal write and checkpoint write); any larger gap is refused. **Protection boundary:** rollback protection covers checkpointed history only. In the one-generation lag window (journal at N+1 written, checkpoint still N), a restore of the Store to the validly signed generation N is indistinguishable from the legitimate state and is accepted; the write N+1 is lost. This is safe for delivery because INV-S6 forbids any effect before the checkpoint covers an authorizing write, so a lost uncheckpointed write never has an issued POST behind it, and a lost outcome or later write can only rewind a task to `reserved` or earlier states that never permit a POST by a run that did not itself confirm the authorizing write (INV-D1, T4r). The loss of non-authorizing work in this window is accepted and reported, not prevented. No stronger mechanism is specified because it would require an anchor write before the Store write, which depends on the unresolved choice D-8/D-11. A missing checkpoint with a non-empty journal fails closed. Initialization requires an owner-attested bootstrap record of the first `(generation, digest)`. Recovery from checkpoint loss or an intentional rollback requires a separately owner-authorized, audited checkpoint reset; the controller MUST NOT perform it.
- **INV-S6** (checkpoint before effect) The checkpoint advance for an authorizing write MUST be confirmed before the authorized effect is issued. A failed or ambiguous advance issues no effect and leaves the task unchanged; the next run applies INV-S5. This needs an anchor the controller can advance on every authorizing write; the consequence is stated in section 8 (D-8, D-11).

### 2.2 CAS and version ownership (INV-C)
- **INV-C1** Every write is a conditional PUT carrying the version token of the last journal this run read or successfully wrote. An unconditional write MUST NOT exist.
- **INV-C2** A run owns a version token only until a write attempt using it returns anything other than a definite success or a definite 409/422 conflict.
- **INV-C3** A run MUST NOT write based on a journal older than the newest one it has observed. After a conflict the run MUST re-read, re-validate (INV-S2, INV-S4), re-derive its mutation from the fresh journal, and retry only if the mutation is still applicable.
- **INV-C4** A mutation MUST be a function of the current journal (read-modify-write), never a replay of a previously computed full blob. Each mutation's state precondition MUST be re-checked on the fresh journal.

### 2.3 Ambiguous writes (INV-A)
A PUT is *ambiguous* when the caller cannot tell whether it applied: timeout, connection reset, 5xx, or an unparseable response.
- **INV-A1** After an ambiguous PUT the run MUST NOT retry it blindly and MUST NOT issue any external effect that depends on that write.
- **INV-A2** The run that issued the PUT, while it is still alive and holds the attempted `mutation_id` and the pre-write journal in memory, MUST perform read-after-write recovery: read the Store, validate it (INV-S2, INV-S4), and classify exactly one of *applied* (the journal contains the intended `mutation_id`; blob equality alone is not sufficient), *not applied* (the journal equals the pre-write journal), or *superseded* (neither).
- **INV-A3** If recovery reads also fail, the run MUST end without any external effect that depends on the write and MUST NOT assume the write did not apply: the durable state is *unknown* (prior or intended) until a later read succeeds. The attempted `mutation_id` and pre-write journal are process-local and MUST NOT be assumed to survive the run; no durable intent record exists. The next run therefore does not perform INV-A2 classification. It MUST begin with read and validation (INV-S2, INV-S4) and then decide from the durable task state alone: because mutations are guarded by state preconditions (INV-C4), an already-applied mutation is not applicable on the fresh read, and an unapplied one is re-derived as a fresh mutation with a new `mutation_id`. A fresh run never inherits an authorization: it MUST NOT issue a POST for a `reserved` task it did not itself confirm (INV-D1); such a task is handled only by T4r. Hence a lost intent can cause a skipped delivery but never a duplicate POST.
- **INV-A4** Each mutation carries a unique `mutation_id` persisted in the journal, making recovery and duplicate callbacks idempotent at journal level.
- **INV-A5** (last confirmed journal) At every point the run's view of durable state is the confirmed journal with its version token. Error and abort handlers (transient errors, CAS exhaustion, budget exit, timeouts after earlier committed writes in the same run) MUST report and act from that view, never from the journal read before the run's first write, and MUST NOT re-write a pre-save blob (INV-N1). An unclassified ambiguous write leaves the affected task *unknown* (INV-A3); other tasks continue from the confirmed journal after a fresh read.

### 2.4 No stale overwrite (INV-N)
- **INV-N1** The controller MUST NOT write a journal derived from a version token that is not the Store's current token (enforced by INV-C1).
- **INV-N2** A restored or replayed older journal MUST be refused even if validly signed, when it is older than the checkpoint or conflicts with it (INV-S4). The only exempt case is the bounded one-generation checkpoint lag of INV-S5, where the restored journal equals the checkpoint; it is accepted and its consequences are limited as stated there.

### 2.5 Physical size guard (INV-B1)
- **INV-B1** The serialized, signed journal MUST NOT exceed `JOURNAL_MAX_BYTES` (a design parameter; the reader's decoder cap and the writer's guard MUST use this same constant). Before every PUT the writer MUST measure the **final signed bytes** and compare them with a `size_limit` supplied by the caller for that mutation, where `size_limit <= JOURNAL_MAX_BYTES`. If the bytes exceed `size_limit` no PUT is sent and a typed error `SizeLimitExceeded` carrying `(bytes, size_limit)` is returned to the caller with no journal change and no external effect. This document does not define which `size_limit` applies to which mutation (contract P3).

### 2.6 Delivery safety (INV-D)
- **INV-D1** At-most-once: a POST for a `(task_id, delivery_id)` is issued at most once ever. Only the run that itself confirmed the T3 write and its checkpoint advance may issue the POST; authorization is never inherited by another run or process. Before the POST the journal MUST durably record `reserved` with a unique `delivery_id` (T3: write confirmed per INV-A5 and checkpoint advance confirmed per INV-S6). Immediately before the POST the run MUST re-read and validate the journal and confirm the task is still `reserved` with the same `delivery_id`. A version-token change caused by an unrelated writer does not revoke the authorization; a changed state of this task does.
- **INV-D2** An ambiguous POST (timeout, reset, 5xx after send) MUST NOT be retried. The task is recorded `delivered_unconfirmed` (T4) and is thereafter resolved only through contract P5.
- **INV-D3** Each task's deadline is a wall-clock value stored in the journal when the task is created. No transition defined here may extend, reset or remove it, and outages never extend it. An unconfirmed task at its deadline is never re-posted (contract P4 governs the escalation).

## 3. State-transition matrix (owned transitions)

All "Persisted write" entries are single CAS journal writes following section 4. "Audit" is the bounded append-only `events[]` record (run id, run attempt, timestamp, `mutation_id`).

| # | Transition | Trigger | Preconditions | Persisted write | Allowed external effects | Retry / recovery | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T3 | `pending` -> `reserved` | The consumer selects a task for delivery (P2) | Deadline not passed; `pre_post_budget_ok` true (P2); no existing `delivery_id`; journal valid (INV-S4) | `reserved` with new `delivery_id`, `reserved_at` | None | Ambiguous PUT: INV-A2/A3. The POST is allowed only when the write is confirmed, its checkpoint advance is confirmed (INV-S6) and the pre-POST re-read passes (INV-D1) | Event with `delivery_id` |
| T4 | `reserved` -> `delivered_unconfirmed` | The single POST was attempted, or its response was lost | T3 and its checkpoint advance confirmed; pre-POST re-read passed; first and only POST for this `delivery_id` | `delivered_unconfirmed`, `posted_at`, outcome class (a definite 2xx is also recorded as `delivered_unconfirmed`) | Exactly one POST, issued before this write | See T4 recovery rules below | Event with outcome class; no response bodies |
| T4r | `reserved` -> `delivered_unconfirmed` | A run reads `reserved` for which it holds no in-flight POST (for example a crash between T3 and T4, or a T4 that did not apply) | `reserved_age > reserved_grace` (P2) | `delivered_unconfirmed` with `post_status=unknown` | None | MUST NOT POST. Resolved via P5 or P4 | Event `recovered_reserved` |
| T4n | `reserved` -> `pending` | A definite pre-send failure is proven (local failure before any socket write, or a definite rejection proving non-send) | Proof recorded | `pending`, `delivery_id` retired, delivery-attempt counter incremented | None | Only for proven non-send. A new `delivery_id` is minted by the next T3 | Event with proof class |
| T9 | terminal -> terminal | Any attempt | - | None | None | Terminal states (`confirmed`, `escalated`, `blocked`) are immutable. This document defines no exception. Any attempt is logged and ignored | Workflow log |
| T10 | any | Journal validation or rollback-check failure (INV-S2, INV-S4, INV-S5) | - | None | None | The run exits failed with an alert and no effect | Workflow log |

**T4 recovery rules** (the durable state after a T4 attempt is decided only by the Store):

| T4 PUT outcome | Durable state | What the same or next run does | Further POSTs |
| --- | --- | --- | --- |
| Definite success | `delivered_unconfirmed` | Adopt the confirmed journal and advance the checkpoint | 0 |
| Ambiguous, classified *applied* (INV-A2) | `delivered_unconfirmed` | Continue. T4r does not apply. T4 is not written again | 0 |
| Ambiguous, classified *not applied* | `reserved` | A later T4r gives `delivered_unconfirmed(post_status=unknown)`. There is no second POST | 0 |
| Ambiguous and recovery reads fail | Unknown (INV-A3) | The next run has no intent record; it reads, validates and decides from durable state only: `delivered_unconfirmed` means do nothing, `reserved` means T4r after `reserved_grace`. It never writes T4 and never POSTs | 0 |
| 409/422 conflict | Fresh journal decides | Re-read, validate, re-derive (INV-C3). If the task is still `reserved` with the same `delivery_id`, write T4 (no POST). If it is already `delivered_unconfirmed`, write nothing. If it is in any other state, write nothing and alert | 0 |

Invariant of the matrix: **a successfully written T4 never leaves the durable state `reserved` in the retained, validated journal history of the run that wrote it**, and T4r is reachable only from a `reserved` state in the current validated durable journal. T4r is decided from that journal alone, never from knowledge of a prior T4 write. One permitted case exists in which a successful T4 is no longer visible: the one-generation checkpoint lag of INV-S5, where a restore to the checkpointed generation N discards an uncheckpointed T4 at N+1 and the validated current journal shows `reserved`. In that case T4r applies from the current journal; the fresh run cannot and need not discover the lost T4. This case never permits a POST: T4r issues none, and authorization is never inherited (INV-D1), so the at-most-once proof is unchanged.

Rules for all rows:
- An external effect is permitted only after the journal write that *authorizes* it is confirmed. The confirmed T3 write authorizes the single POST; the T4 write records the outcome after the POST and does not authorize it. No row requires the write that records an effect to precede that effect (INV-A1).
- Authorizing writes also require the checkpoint rule (INV-S6).

## 4. Journal write procedure (normative sequence)

1. Read the journal and checkpoint; validate (INV-S1, INV-S2, INV-S3, INV-S4); remember `(version_token, generation)`.
2. Compute mutation `m` with a fresh `mutation_id` as a function of the journal, with its state precondition checked (INV-C4).
3. Serialize and sign. Measure the final signed bytes against the caller's `size_limit` (INV-B1). On excess return `SizeLimitExceeded` to the caller and stop.
4. PUT with the version token.
5. Outcome: definite success: adopt the new token (the journal is now confirmed, INV-A5) and advance the checkpoint (INV-S5, INV-S6). 409/422 conflict: go to step 1, bounded by the caller's retry limit (P6). Ambiguous: recovery per INV-A2/A3. Other failures: reported to the caller as typed errors (P6). On any abort the run reports from the confirmed journal (INV-A5).

## 5. Fault-injection harness (applies to every FA test)

- **Seeded fake Store:** a versioned in-memory object with a seeded PRNG for fault schedules, a signing key held by the test, and fault modes: drop request (not applied), apply and drop response (applied, response lost), 5xx, timeout, 409, persistent 409, read failure, and restore to an older validly signed journal.
- **Fake clock:** monotonic and wall-clock time are test-controlled. No real sleeping, no real network.
- **Fake trust anchor:** an advance-only checkpoint store with fault modes: advance fails, advance ambiguous, missing, and lagging.
- **Fake external API:** counts POSTs per `(task_id, delivery_id)` and records the journal state observed by the Store at each POST instant.
- **Inter-process model:** "process 2 starts fresh" means a new controller instance with no in-memory state that sees only the Store and the anchor.
- Assertions are on observable fake state only. Tests are specified here; none has been run.

## 6. Fault-injection test matrix

"POSTs" counts POSTs for the task under test. Time budgets are harness settings, not policy.

| ID | Case | Setup | Injection point | Expected journal transition | POSTs | Observable assertion |
| --- | --- | --- | --- | --- | --- | --- |
| FA-01 | Ambiguous PUT, applied | 1 `pending` task | Store applies T3, drops the response | `pending` -> `reserved` found by INV-A2 | 1, only after recovery confirms applied | Journal holds the `mutation_id`; no second PUT of the same blob; POST only after checkpoint advance |
| FA-02 | Ambiguous PUT, not applied | as FA-01 | Store drops the request | Stays `pending`, then T3 by fresh read-modify-write | 1, after a confirmed T3 | Generation rose by exactly 1; POST only after the confirmed write |
| FA-03 | Ambiguous PUT, superseded | Another writer advances the journal | Response lost plus external write | Mutation re-derived from the fresh journal; dropped if not applicable | 0 or 1 per applicability | The other writer's change is not overwritten (INV-N1) |
| FA-04a | Recovery reads fail, PUT had applied | as FA-01 | Store applies the PUT, drops the response, then all reads fail | Unknown until a read succeeds; durable `reserved` | 0 | No POST and no assumption of rollback; process 2 starts fresh with no `mutation_id` or pre-write journal, reads `reserved`, applies T4r after `reserved_grace`, still 0 POSTs |
| FA-04b | Recovery reads fail, PUT had not applied | as FA-01 | Store drops the request, then all reads fail | Unknown until a read succeeds; durable `pending` | 0 then 1 | No POST in the failing run; process 2 starts fresh with no process-local intent, reads `pending`, performs a fresh T3 with a new `mutation_id` and `delivery_id`, then exactly 1 POST |
| FA-04c | Intent lost, pre-write journal replaced | as FA-04a | As FA-04a/b, plus another writer changes the journal before process 2 starts | Process 2 decides from durable state only | 0 from the unknown window; at most 1 after a new confirmed T3 | No classification is attempted without the lost intent; no POST for a `reserved` task process 2 did not confirm |
| FA-05 | Stale CAS | Two runs read the same token | Run B writes first | Run A gets 409, re-reads, re-derives | Total across runs 1 | Final journal contains both mutations, or A's is dropped as inapplicable; never A's stale blob |
| FA-07 | Crash after T3, before POST | `reserved` task, T3 and checkpoint confirmed | Process kill | On restart T4r gives `delivered_unconfirmed(post_status=unknown)` | 0 on restart | Zero POSTs after restart |
| FA-08 | Crash after POST, before T4 write | POST sent, no T4 write | Kill after POST | Same as FA-07 | Total 1 | Total POST count across both runs is 1 |
| FA-09 | Ambiguous POST | POST times out after send | Timeout | `delivered_unconfirmed` (T4) | 1, no retry | No second POST in this or any later run |
| FA-09b-applied | T4 PUT applied, response lost | `reserved` task, T3 and checkpoint confirmed | POST ambiguous or successful; Store applies T4 and drops the response (variant: recovery reads also fail) | Durable `delivered_unconfirmed` | 1 total | Next run reads `delivered_unconfirmed`, does not apply T4r and does not write T4 again; 0 further POSTs |
| FA-09b-unapplied | T4 PUT not applied | as above | Store drops the T4 request (variant: reads fail afterwards) | Durable `reserved`; then T4r gives `delivered_unconfirmed(post_status=unknown)` | 1 total | Next run reads `reserved`, applies T4r; 0 further POSTs; no pre-write of T4 was needed |
| FA-09b-conflict-rederive | T4 PUT gets a conflict | as above | The T4 PUT returns 409; one variant has an unrelated task's write landing in between; a second variant has this task already `delivered_unconfirmed` | Fresh journal re-read. Task still `reserved` with same `delivery_id`: T4 re-derived and written, durable `delivered_unconfirmed`. Already `delivered_unconfirmed`: no write | 1 total | After the successful re-derived T4 the durable state is `delivered_unconfirmed`, never `reserved`; T4r is not applied; 0 further POSTs |
| FA-09c | Reservation authorizes the POST | `pending` task | Observe the Store at the POST instant | `reserved` is durable at POST time; `delivered_unconfirmed` appears only afterwards | 1 | At the POST instant the Store and anchor already cover `reserved` with the `delivery_id`; a POST without a confirmed `reserved` write and anchor advance fails the test |
| FA-27 | Concurrent independent tasks | 10 tasks, one with ambiguous PUT, one with a Store outage | Mixed | Others progress independently from the confirmed journal | At most 1 per task | No task is blocked by another's ambiguity or fault except through CAS retries (INV-A5, INV-C3) |
| FA-28 | Validly signed older journal restored between processes | Process 1 advances to generation N and the anchor; the Store is restored to a validly signed generation N-k (variant: generation N with a different digest); process 2 starts fresh | Store read | Rejected whole before any mutation (INV-S4, INV-N2) | 0 | Zero PUT and zero POST; anchor unchanged; alert recorded |
| FA-28b | Restore inside the checkpoint lag window | Process 1 writes generation N+1, crashes before the anchor advance (anchor at N); the Store is restored to the signed generation N; process 2 starts fresh | Store read | Journal accepted as matching the checkpoint (INV-S5 protection boundary); work from N+1 is lost and re-derived. Variants: (i) N+1 was T3, (ii) N+1 was T4 | 0 POSTs for variant (i) since no POST preceded the anchor advance; 0 further POSTs for variant (ii) | No PUT or POST based on N+1; variant (i) task is `pending` and later gets a new `delivery_id`; variant (ii) task is `reserved` in the validated current journal and gets T4r after `reserved_grace` (permitted by the matrix invariant, which keys T4r to the current validated journal, not to the lost T4); the fake external API counts exactly 1 POST in total for the task across both processes, and process 2 issues none; a restore to N-1 in the same setup is refused (INV-S4) |
| FA-29 | Corrupted or unsigned journal | Bad signature or schema | Store read | Rejected whole | 0 | No write; run fails closed (INV-S2, T10) |
| FA-30 | CAS retry exhaustion | Persistent 409 | Store | No change | 0 | Retries stop at the caller's limit; run exits without effects; reports from the confirmed journal |
| FA-37 | Missing checkpoint, non-empty journal | Anchor absent, generation above 0 | Anchor read | Run fails closed | 0 | Zero PUT and zero POST; owner alert; no automatic bootstrap (INV-S5) |
| FA-38 | Checkpoint lags by one generation | Crash after a journal write, before the anchor advance | Restart | Anchor advanced after `prev_digest` verification; run proceeds | per normal | Anchor equals journal generation; no rewind accepted (INV-S5) |
| FA-39 | Checkpoint gap larger than one | Journal ahead of the anchor by 2 or more, or the `prev_digest` chain does not reach it | Anchor read | Rejected whole | 0 | Zero PUT and zero POST; alert (INV-S4) |
| FA-45 | Transient error after earlier committed writes | One run commits write 1 (task X) and write 2 (task Y), then a 5xx on a later read aborts the run | Store, after two committed writes | The run reports and exits from the confirmed journal | 0 in this run | Reported state equals the Store's journal; generation equals the last committed write; no PUT carries the run's initial blob or token after the first write (INV-A5, INV-N1) |
| FA-46 | CAS abort after partial writes | Two writes commit, the third gets persistent 409 until the retry limit | Store | The two committed writes are retained | 0 | No PUT derived from the original blob; final state equals the confirmed journal (INV-A5, INV-C3) |
| FA-47 | Checkpoint advance fails before POST | T3 confirmed, then the anchor advance fails or is ambiguous (variant: after a confirmed advance and a POST, the Store is restored to the pre-T3 journal) | Anchor | Failed advance: task stays `reserved`, no POST; next run applies INV-S5 then T4r. Variant: restored journal refused whole | 0 additional | No POST before the anchor covers the authorizing write (INV-S6); the restore variant causes zero PUT and zero second POST |
| FI-25 | Over-limit write guard | A mutation whose final signed bytes exceed the supplied `size_limit` (variants: limit equals `JOURNAL_MAX_BYTES`; limit below it; bytes exactly at the limit) | Pre-PUT | No journal change | 0 | Zero PUT requests above the limit; typed `SizeLimitExceeded`; one PUT at exactly the limit; the writer cap and decoder cap are the same constant (INV-B1) |

## 7. Interface contracts for consuming documents

A consumer may rely on the following. This document refers to consumers only through these named parameters and never defines their policy.

| ID | Kind | Contract |
| --- | --- | --- |
| P1 | Provided | The write procedure (section 4) with its guarantees: confirmed-journal view, rollback refusal, CAS re-derive, ambiguous-write classification. |
| P2 | Required input | At T3 the caller supplies `pre_post_budget_ok` (a boolean computed from remaining execution time; this document defines no budget) and `reserved_grace` (a duration that MUST be at least the maximum POST timeout plus one write and one checkpoint advance). |
| P3 | Required input | For every mutation the caller supplies `size_limit <= JOURNAL_MAX_BYTES`. This document does not say which limit applies to new-task admission or to existing-task mutations. |
| P4 | Required input | Deadline handling: the consumer defines the transitions that move non-terminal tasks to a terminal state when the stored deadline (INV-D3) has passed. Each is one section-4 write. None may issue a POST of an already-reserved delivery. |
| P5 | Required input | Resolution of `delivered_unconfirmed`: the consumer defines the transitions out of it. Each is one section-4 write, and none may issue a POST for the same `delivery_id`. |
| P6 | Required input | The CAS retry limit, recovery-read limit and the mapping of non-conflict failures to typed errors. |
| P7 | Provided | Typed errors `SizeLimitExceeded`, `RollbackRefused`, `JournalInvalid`, `CheckpointUnavailable`. |
| P8 | Provided | A consumer's extra precondition or extra write on an owned transition MUST be written in the consumer's document as an extension of that transition. It MAY only narrow when the transition fires and MUST NOT remove a precondition defined here. |

## 8. Unresolved owner decisions (class I) and activation hold

- **D-8** Trust anchor for the checkpoint (INV-S4, INV-S5, INV-S6). Options: (a) a checkpoint ledger in a separate protected location written with a distinct credential and signing key that the controller's Store credential cannot overwrite; (b) a signed append-only git ref or tag protected by a ruleset forbidding deletion and force-push; (c) an owner-held external anchor updated by the owner. This document selects none, creates no credential, secret, token, ref protection or permission, and requests none.
- **D-11** Consequence of INV-S6 for D-8. INV-S6 needs an anchor the controller can advance on every authorizing write. An owner-held anchor (c) cannot do that. Until the owner chooses (a) or (b), or explicitly accepts a weaker per-window anchor with a documented duplicate-POST risk, any implementation MUST stay fail-closed: no T3 write may be followed by a POST, so only read, reconcile and escalate paths may run. This holds regardless of any other acceptance. Any choice requires separate owner authorization.
- Liveness cost: if an anchor advance fails after T3, no POST is issued, T4r applies, and the delivery is never attempted (FA-47). This is accepted as the fail-closed behaviour of this document and is reported rather than hidden.
- Bootstrap and reset authority for the checkpoint is owner-attested only (INV-S5). Whether this is acceptable is an owner decision.

## 9. Ownership and relocation ledger

Each ID below has exactly one authoritative home: this document. IDs not listed belong to other documents.

| Old ID (parked PR #228 text) | Home here | Change |
| --- | --- | --- |
| INV-S1, S2, S3 | same | unchanged meaning |
| INV-S4, S5 | same | INV-S5 refers to INV-A5 instead of INV-A2 for "confirmed" |
| INV-S6 | same | restated without referring to a specific anchor |
| INV-C1..C4 | same | INV-C3 adds INV-S4 revalidation; INV-C4 adds precondition re-check |
| INV-A1..A4 | same | INV-A3 restated without rollback claim; INV-A2 adds INV-S4 validation |
| INV-A5 | same | unchanged |
| INV-N1, N2 | same | INV-N2 cites INV-S4 |
| INV-D1 | same | adds checkpoint confirmation and pre-POST re-read |
| INV-D2 | same | the T4 outcome write is explicitly after the POST |
| INV-D3 | same | split: storage and non-extension stay here, escalation transitions are contract P4 |
| INV-B1 | same | parameterized by caller-supplied `size_limit`; new typed error name |
| INV-B2..B5, INV-F1..F4 | not here | not owned by this document |
| T3, T4, T4r, T4n | same | T3 takes `pre_post_budget_ok` and `reserved_grace` as inputs; T4 gains the recovery table |
| T9, T10 | same | T9 no longer lists an exception |
| T1, T1d, T2, T5..T8, T11 | not here | not owned by this document |
| D-8, D-11 | section 8 | unresolved, owner decision |
| FI-01..FI-05 | FA-01..FA-05 | FI-04 is FA-04a/b |
| FI-07, 08, 09 | FA-07, 08, 09 | unchanged |
| FI-09b (one row, then applied/unapplied) | FA-09b-applied, FA-09b-unapplied, FA-09b-conflict-rederive | conflict variant added |
| FI-09c | FA-09c | unchanged |
| FI-27, 28, 29, 30 | FA-27, 28, 29, 30 | FA-30 uses caller retry limit (P6) |
| FI-37, 38, 39 | FA-37, 38, 39 | unchanged |
| FI-45, 46, 47 | FA-45, 46, 47 | unchanged |
| FI-25 | FI-25 | name kept; limit is caller-supplied |
| G-1 | closed here, review pending | addressed by INV-A5, FA-45, FA-46 |

## 10. Mapping of findings to this document

| Thread | Finding | Invariants and sections | Tests |
| --- | --- | --- | --- |
| #228 `PRRT_kwDOUdC8Ds6yBbwQ` | Cross-run rollback trust anchor | INV-S4, S5 (protection boundary), S6, N2; D-8, D-11 | FA-28, FA-28b, FA-37, FA-38, FA-39, FA-47 |
| #228 `PRRT_kwDOUdC8Ds6yBbwf` | T3 authorizes the POST, T4 records the outcome | INV-D1, D2; T3, T4; section 3 rules | FA-07, FA-08, FA-09, FA-09c |
| #228 `PRRT_kwDOUdC8Ds6yBbwn` | Unknown durable state after an ambiguous PUT and failed reads | INV-A1, A2, A3 (state-based restart recovery) | FA-04a, FA-04b, FA-04c |
| #228 `PRRT_kwDOUdC8Ds6yHAJ-` | Ambiguous T4 PUT, applied versus unapplied | T4 recovery rules, T4r | FA-09b-applied, FA-09b-unapplied |
| #228 `PRRT_kwDOUdC8Ds6yNm7l` | T4 conflict variant must not leave durable `reserved` | INV-C3, C4; T4 recovery rules | FA-09b-conflict-rederive |
| #219 `PRRT_kwDOUdC8Ds6xg8Vi` | Stale pre-save journal returned after committed writes | INV-A1..A5, INV-N1 | FA-01..FA-04b, FA-45 |
| #219 `PRRT_kwDOUdC8Ds6xn4us` | Stale original blob after partial writes, CAS conflict and abort | INV-A5, C1, C3, C4, N1 | FA-05, FA-30, FA-46 |
| #219 `PRRT_kwDOUdC8Ds6xtjVE` (secondary) | Journal outgrows the decoder cap | INV-B1 (physical guard only) | FI-25 |
| #219 `PRRT_kwDOUdC8Ds6x0PNt` (secondary) | Transient failure bypasses the stored deadline | INV-D3, contract P4 | FA-27, FA-30 |
| #219 `PRRT_kwDOUdC8Ds6x0phT` (secondary) | A transient lookup aborts work on existing tasks | INV-A5, contract P6 | FA-27, FA-45 |
| #219 `PRRT_kwDOUdC8Ds6x7kSU` (secondary) | A size refusal aborts existing-task work | INV-B1 typed error, contract P3 | FI-25, FA-27 |

The #228 cursor thread `PRRT_kwDOUdC8Ds6yHAKC` is not owned here. All 12 threads on #219 and all 12 on #228 stay open and unresolved on their original PRs; this document resolves none of them and asserts no acceptance.

## 11. Acceptance criteria and non-goals

Acceptable for the next step only when all hold: (1) exact-head required CI is green; (2) independent Codex review has accepted the exact head, plus an owner-run OpenCode or Jules review (DX20); (3) every A-owned thread in section 10 is dispositioned with evidence; (4) every FA/FI row names an invariant of this document and every cited ID exists here; (5) D-8 and D-11 are answered by the owner in writing, or the fail-closed hold of section 8 is kept; (6) production execution, activation and any POST-issuing mode remain separately owner-authorized.

This document changes no workflow, script, application code, secret, token or permission, and does not authorize merging PR #219, activating the controller, or any production action. Migration 0057 production execution remains prohibited.
