# PHASE2_GAP_ANALYSIS.md

Independent comparison of `ARCHITECTURE_REVIEW.md` v1.3 §4/§4a/§4b/§7/§7a/§7b/§7c/§8/§9/
§10/§10a/§11/§12/§29/§36/§38/§40 against the Phase 1 implementation baseline
(`2bbf0a5`) and the Phase 2 scope requested. Performed before writing any code, per the
mandatory Step 1.

## 1. Already satisfied by Phase 1 (no Phase 2 work needed)

- `ManagedAsset` identity/lifecycle anchor, shared-PK inheritance pattern, `asset_type`
  enum, `replaces_asset_id` (§4/§4a) — reused as-is; Phase 2 adds `rack`/`equipment` as
  the first real subtype tables riding this anchor. No change to `ManagedAsset` itself.
- Location hierarchy (Organization→Room) — `Room` is the spatial container Phase 2's
  placement/spatial tables FK into.
- Optimistic concurrency pattern (`version`/`If-Match`, `app/application/concurrency.py`)
  — reused directly for `Rack`/`Equipment`/`FloorPlan`.
- Locked close-then-open placement transaction pattern (§7c) — Phase 1's `concurrency.py`
  module docstring already documents this is exactly what `RackPlacement`/
  `EquipmentPlacement` will use; Phase 2 implements it for the first time.
- Audit (`write_audit_log`), Outbox (`write_outbox_event`), Idempotency
  (`get_or_claim`/`complete_claim`/`release_claim`) — reused as-is, no second mechanism.
- RBAC (`require_permission`, global-only per H7 Option B) — reused as-is; Phase 2 only
  adds new permission codes to the existing table, never a new authorization system.

## 2. Requirements needing implementation (this phase)

Rack/Equipment domain, RackPlacement/EquipmentPlacement with full §7a/§7b/§7c/§8
enforcement (including the v1.3 `side IS NOT NULL` correction), canonical mm coordinate
system, FloorPlan/SpatialObject model, secure SVG import pipeline, import diagnostics,
authoritative-vs-imported separation, rack elevation (derived, not stored, per §12),
2D floor-plan view, spatial validation engine, APIs, tests, docs.

## 3. Ambiguities resolved without escalation

**3.1 — `RackModelRevision`/`EquipmentModelRevision`, `SpatialLayer`/`SpatialObject`,
`FloorPlan`, `FloorPlanImportJob`/`Candidate` have no published column lists anywhere in
`ARCHITECTURE_REVIEW.md`** (confirmed by exhaustive grep — only narrative references and
partial inline sketches exist, e.g. §8's `RackPlacement` block references
`SpatialObject`/`spatial_layer_id` without ever defining those tables). This is the same
category of gap Phase 1's `PHASE1_BASELINE.md` AD1 already established a precedent for
(v1.0's Organization/Room schema was dropped from the v1.1 rewrite and never
re-published; Phase 1 treated it as a documentation gap, not a contradiction, and
authored the implementation from the narrative requirements). Resolution: author these
schemas directly from what *is* specified — §7/§8's inline column sketches for
`RackPlacement`/`EquipmentPlacement`, §9's coordinate model, §10/§10a's pipeline stages,
§11's diagnostics fields — rather than escalate for every missing column list. Documented
here, not done silently.

**3.2 — `RackModelRevision`/`EquipmentModelRevision` "revision" semantics.** §4b names
`model_revision_id FK→RackModelRevision NOT NULL` but never explains what varies across
revisions of the same model. Resolved by analogy with the rest of the document's
established pattern (append-only history via immutable rows a foreign key points at,
e.g. `AuditLog`, `TelemetryReading`): `RackModel` (manufacturer + model name, the
catalog entry) has one or more `RackModelRevision` rows (height/width/depth, etc.) —
existing `Rack` rows keep pointing at the revision they were created against even if the
manufacturer later republishes corrected dimensions under a new revision. This is the
minimal structure that satisfies "NOT NULL FK to a *revision*" literally rather than
collapsing it to a single mutable row on `RackModel` itself, which the architecture's own
wording rules out.

**3.3 — `FloorPlanRevision` vs. `FloorPlan.status`.** §8 says "exactly one ACTIVE
FloorPlan at a time; prior ones retained, status=superseded" — this describes revisioning
via multiple `FloorPlan` rows sharing a `room_id`, not a separate `FloorPlan` +
`FloorPlanRevision` parent/child pair. Resolved: `FloorPlan` itself carries
`revision_number` (monotonic per room) and `status` (`draft`/`active`/`superseded`); no
separate revision table, since the architecture's own wording already describes this
shape.

**3.4 — Full OS-level process isolation for the untrusted-file parser (§10a).** The
architecture requires "a narrowly-scoped worker/subprocess" with no DB/Redis credentials,
no network route, and OS-level resource limits (cgroup/ulimit). Implementing genuine
process-level sandboxing (a separate unprivileged container/process with its own
credential-free environment) is an infrastructure concern on the same order as Docker
Compose runtime validation, which Phase 1 explicitly could not runtime-validate in this
sandbox (no Docker daemon; `PHASE1_DEVIATIONS.md` D1). Resolution, applying the same
disclosed-limitation pattern rather than silently faking isolation: Phase 2 implements
every *content-level* control §10a requires in-process — `defusedxml`-based parsing with
DTD/external-entity resolution disabled (the XXE defense), a hard element-count cap and
file-size cap (bounding both memory and wall-clock time without needing a separate
timeout mechanism), `<script>`/event-handler/external-reference stripping, and a
Sanitized Intermediate Representation that is the only thing crossing back into the rest
of the pipeline — but does **not** run the parser in a separate OS process/container with
its own credential-free identity in this pass. This is recorded as a known limitation
(§10a's own acceptance criterion already anticipates this: "architecturally defined, not
yet implementation-verified" was Phase 1's own wording for the analogous case), not
claimed as done. See `PHASE2_IMPLEMENTATION_REPORT.md` §Known Issues.

**3.5 — `object_class`/`object_id` polymorphic pattern (§4) is not exercised by Phase 2.**
Telemetry/Alarm/Event (the tables that need it) are explicitly out of Phase 2 scope. No
Phase 2 table introduces a new polymorphic reference; `RackPlacement`/`EquipmentPlacement`
use real FKs throughout, consistent with §38's stated preference.

## 4. No contradictions found

Nothing in the Phase 2 prompt's scope conflicts with v1.3. The prompt's explicit
exclusions (no CFD, no 3D, no telemetry, no power/network topology, no microservices)
match the architecture's own phase sequencing (§47) — Phase 2 is squarely "Physical
Infrastructure & Spatial Model Foundation," which §47 places before Power (§13)/Network
(§15)/Telemetry (§16) work.

## 5. Migration implications

One additive migration (no edits to Phase 1's `e9fd19228f19`/`0002_seed`/
`0003_correction`): new tables only, new RBAC permission rows (idempotent, same pattern
as `0002_seed`'s `DEFAULT_ROLE_PERMISSIONS` import), no change to any existing table's
columns or constraints. Upgrade path from the Phase 1 baseline is therefore
structurally low-risk — nothing Phase 2 adds can invalidate existing Phase 1 rows.

## 6. Future-compatibility risks identified and addressed

- `EquipmentPlacement.side IS NOT NULL` (v1.3's F1 correction) is carried forward
  exactly, with a dedicated regression test attempting the NULL-bypass the correction
  closed, per the Phase 2 prompt's own explicit instruction not to reintroduce it.
- `RackPlacement`/`EquipmentPlacement` use the same `FOR UPDATE`-based locked
  close-then-open transaction as the architecture specifies, not a simplified version,
  so the concurrency guarantee is real, not decorative.
- Rack elevation is computed from `EquipmentPlacement` on every read (§12/§7c's closing
  line) — no `RackElevationSnapshot`-style cache table is introduced.
