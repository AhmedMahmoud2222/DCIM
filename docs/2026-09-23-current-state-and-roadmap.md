# Current State & Roadmap Addendum

**Dated:** 2026-09-23
**Pinned against:** `claude/phase10a-lifecycle-backend` @ `591519b25876bb92c3d48d8ee530405c17ce4ad8` (backend
Alembic head `0021_catalog_rbac_seed`), cross-checked against `origin/main` @ `4964d8d` (Alembic head
`0013_metric_mapping_managed_asset`) and `origin/codex/commercial-ui-uplift-v1` @ `5f39b20` (Alembic head
`0016_network_runtime_defaults`).

**What this document is.** A snapshot of what actually runs today, across the repository's active
branches, and what is still design or plan. It does not replace, re-litigate, or renumber any historical
phase report — see [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §47 for the original Phase 0–14
roadmap this repository was built from, and the dated `PHASE*.md` reports at the repository root for each
phase's own completion record. Treat those as the historical record; treat this file as the thing to reread
when a branch's status changes, since branch state moves faster than a phase report gets rewritten.

## 1. Branches: what code actually exists where

This repository currently develops across three distinct lineages that a new contributor needs to tell
apart before trusting any capability claim:

| Branch | Backend Alembic head | Contains |
|---|---|---|
| `main` | `0013_metric_mapping_managed_asset` | The historical numbered roadmap (§2 below) through Telemetry/MVP retention. **No network topology or 3D layout.** No Asset Catalog Designer. |
| `codex/commercial-ui-uplift-v1` | `0016_network_runtime_defaults` | Everything `main` has, **plus** network topology, physical connections, and the 3D room layout (merged via PR #9, "implement-network-topology-and-3d-layout") and its own follow-up security fixes. This is the "commercial UI uplift" branch. No Asset Catalog Designer. |
| `claude/phase10a-*` stack (this branch included) | `0021_catalog_rbac_seed` | Forked from the same point `codex/commercial-ui-uplift-v1` was built from (commit `224f122`, right after PR #9 merged) — so it **includes** network topology/3D — **plus** the new, still entirely draft and unmerged, Asset Catalog Designer (§4 below). It has **not** picked up `codex/commercial-ui-uplift-v1`'s later merge of `main`'s own follow-up security fix (`4964d8d`, frontend Vite/esbuild remediation); that fix is not yet ported onto this lineage. |

Practical consequence: if you check out `main`, you will not find `/api/v1/network/*` or any 3D view. If
you check out `codex/commercial-ui-uplift-v1`, you get network + 3D but not the catalog designer. If you
check out any `claude/phase10a-*` branch, you get network + 3D **and** the catalog designer's backend, but
the catalog designer has no UI yet (PR-4, §4) and none of it is merged anywhere.

## 2. Implemented MVP capabilities (historical Phase 0–14 roadmap)

The platform is a modular monolith: FastAPI (async, Python 3.11) + PostgreSQL 16 + Redis/Celery on the
backend, React 18 + TypeScript + Vite + Tailwind + TanStack Query on the frontend. Full architectural
rationale is `ARCHITECTURE_REVIEW.md` (canonical spec, v1.3) and its companion revision/red-team/validation
documents — unchanged and not restated here.

Against `ARCHITECTURE_REVIEW.md` §47's original phase table, the following is **implemented and tested**
(present on `main`, and therefore on every branch downstream of it):

- **Phase 1 — Foundation:** authentication, RBAC, audit log (partitioned, append-only), the transactional
  Outbox, migrations, CI, observability/configuration scaffolding.
- **Phase 2 — Location + Inventory:** Organization → Site → Building → Floor → Room hierarchy,
  `ManagedAsset` identity/lifecycle anchor, search/filter.
- **Phase 3/4 — Rack & Equipment Models:** `RackModel`/`EquipmentModel` + revisions, `EquipmentPlacement`,
  computed rack elevation. (This legacy authoring path is what Phase 10A's Asset Catalog Designer, §4, is
  building a structured replacement for — both paths coexist today; see §4's bridge note.)
- **Phase 5/6 — Spatial + Floor Plan Import:** `FloorPlan`, `SpatialObject`, `RackPlacement`, calibration,
  grid/snap, an importer with a review queue and a quarantine/isolated-parse security boundary for
  untrusted uploads (SVG/raster).
- **Phase 7 — Power:** `PowerNode`, `PowerConnection`, `PowerCapacity`, topology, concurrency-safe edits.
- **Phase 8 — Integrations + Collectors:** protocol/driver/vendor-profile framework, ICMP/SNMP/REST
  drivers, `Collector` identity and heartbeat, discovery.
- **Phase 9 — Telemetry (DCIM MVP v0.1):** an isolated `edge_collector/` package (SQLite-buffered,
  HMAC-signed, bounded retry/backoff) forwards normalized readings to central PostgreSQL
  (`TelemetryReading`, occurred-at/received-at split, dedup key); raw readings retained 365 days, then
  rolled into daily aggregates by a controlled retention task. See
  `docs/superpowers/specs/2026-09-18-dcim-mvp-v0.1-design.md` for the exact designed data flow and its
  explicit deferred-scope list (no SNMPv3, no Timescale/Kafka, no ML/AI, no multi-region — unchanged).
- **Phase 10 — Alarm + Event:** threshold/availability rules, `ACTIVE → ACKNOWLEDGED → CLEARED` lifecycle,
  hysteresis, audited transitions. **Do not confuse this historical "Phase 10" with "Phase 10A" (§4) — the
  shared "10" is coincidental naming, not a subdivision; Phase 10A is an unrelated, much later workstream.**
- **Dashboard / Operational UI:** capacity summaries, collector health, active alarms, latest telemetry
  values, equipment operational view — composed from the domains above.

**Additional, only on `codex/commercial-ui-uplift-v1`** (not on `main`, not yet ported back to it):

- **Network topology:** devices, interfaces, physical connections with provenance (operator vs. collector
  authority — collector observations are never silently promoted to operator authority), and a topology
  endpoint.
- **3D room layout:** a CSS-perspective 3D workspace (deliberately not a rendering framework — see
  `docs/NETWORK_3D_INCREMENT.md`) reading the same `/spatial/rooms/{id}/view` projection the 2D floor plan
  uses.
- **Network path tracing** (`GET /network/trace/{device_id}`, `GET /network/equipment/{id}/context`):
  returns a **modeled physical path** to a core device, with an explicit `state`/`statement` pair
  distinguishing "complete modeled path" from "inventory does not prove a complete modeled path" — this is
  never a live reachability test. The endpoint's own response literally states: *"Complete modeled physical
  path to a core device; this is not a live reachability test."* No LLDP/CDP or SNMP topology discovery,
  no LACP, no VLAN/VRF catalog, no routing, no live utilization, and no redundant-path policy exist — all
  explicitly deferred, per `docs/NETWORK_3D_INCREMENT.md`.

**Explicitly not implemented anywhere in this repository, on any branch:**

- Phase 12 (Capacity/`CapacitySnapshot`), Phase 13 (Reporting/Advanced Operations), Phase 14 (AI Readiness).
- Full site-scoped human RBAC — `ARCHITECTURE_REVIEW.md` §32a records the decision as Option B: global
  authorization ships first, site scoping is additive later behind a stated trigger condition. This is a
  live, recorded, open item, not an oversight.
- Any thermal/CFD simulation, environmental overlay, or synthetic heat map — `ARCHITECTURE_REVIEW.md` and
  `docs/NETWORK_3D_INCREMENT.md` both scope this out explicitly; only a data-model placeholder exists for
  thermal profiles, no simulation.
- Packet-level network reachability of any kind (see the network path tracing note above).
- Any autonomous operation — every write path in this system is a human-initiated, RBAC-gated HTTP request;
  there is no scheduler, agent, or background process that changes inventory, topology, or configuration on
  its own initiative anywhere in this codebase.

## 3. Setup and test commands

See the root [`README.md`](../README.md) for the full, current setup/test/lint walkthrough — not
duplicated here to avoid the two ever disagreeing. One addition worth calling out explicitly because it is
easy to trip over: `backend/tests/unit/test_monitoring_policy_contract.py` imports the sibling
`edge_collector/` package (deliberately not installed into the backend distribution — it is validated as an
independent package, per `docs/superpowers/specs/2026-09-18-dcim-mvp-v0.1-design.md`'s isolation boundary).
Running `pytest` from `backend/` without `PYTHONPATH` set to the repository root fails that one file's
collection with `ModuleNotFoundError: No module named 'edge_collector'` — this is not a code defect; it is
CI's own documented invocation (`.github/workflows/ci.yml` sets `PYTHONPATH: ${{ github.workspace }}` for
exactly this reason). See README's Tests section for the corrected local command.

## 4. Phase 10A — Asset Catalog Designer (new workstream, entirely draft)

**This is not part of the historical Phase 0–14 roadmap in §2.** It is a separate, later-numbered
workstream that happens to share the digit "10" with the historical Alarm+Event phase — coincidental
naming, not a subdivision or continuation of it. It sits alongside two sibling workstreams, **Phase 10B
(Network Operations)** and **Phase 10C (Spatial Digital Twin)**, both of which are named placeholders only:
no specification, no plan, no code exists for either, and every PR in Phase 10A's own plan explicitly
excludes touching their surfaces.

**Authoritative documents** (both dated 2026-09-23, both read in full before any Phase 10A code was
written):
- Specification: `docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md`
- Implementation plan: `docs/superpowers/plans/2026-09-23-phase-10a-asset-catalog-designer-plan.md`

**Purpose:** replace the legacy `RackModel`/`EquipmentModel` flat authoring path (Phase 3/4, §2 above) with
a structured, versioned `CatalogModel` → `CatalogModelRevision` (draft/published/retired) → child template
(network ports, power supplies, monitoring metrics) design, bridged to the legacy tables at publish time so
existing rack/equipment installation flows keep working unchanged during the transition.

**Status taxonomy** — every claim below is filed under exactly one of these, deliberately, so "designed"
and "shipped" are never conflated:

| Status | Meaning here |
|---|---|
| **Implemented, draft-PR** | Code exists, is tested, and is currently under review on an open draft PR — not merged, not on `main`. |
| **Approved design, unimplemented** | The design is settled and authoritative in the specification, but zero code exists for it — no migration, no model, no endpoint. |
| **Planned increment** | Scoped in the implementation plan, not yet started. |
| **Deferred / out of scope** | Named but explicitly excluded from every currently-planned PR. |

**PR dependency chain — every one of these is draft, unmerged, not marked ready for review:**

| PR | Scope | Branch | Status |
|---|---|---|---|
| PR-1 ([#14](https://github.com/AhmedMahmoud2222/DCIM/pull/14)) | Schema + DB guards (8 core tables, lifecycle/identity-lock triggers) | `claude/phase10a-catalog-schema` | Implemented, draft PR |
| PR-2 ([#15](https://github.com/AhmedMahmoud2222/DCIM/pull/15)) | RBAC seed, closes legacy catalog-authoring paths to Administrator-only | `claude/phase10a-rbac-closure` | Implemented, draft PR |
| PR-3 ([#16](https://github.com/AhmedMahmoud2222/DCIM/pull/16)) | Lifecycle API: draft create/edit/publish/clone/retire/compare, child templates | `claude/phase10a-lifecycle-backend` | Implemented, draft PR — two correction rounds applied (draft-read leak closure, draft/child-mutation/revision-number concurrency, `DELETE` concurrency gap); 639 passed, 0 failed as of this branch's HEAD |
| PR-4 | Core administrator UI (React) | not yet branched | Planned increment |
| PR-5 | Graphics and markers | not yet branched | Planned increment |
| PR-6 | Portable JSON import/export (`CatalogImportJob`) | not yet branched | Planned increment |
| PR-7 | Installed-asset migration and instance provenance (`CatalogComponentOverride`) | not yet branched | Approved design, unimplemented (see below) |
| PR-8 | Monitoring-template seeding, regression, documentation | not yet branched | Planned increment |

This document (and the branch it lives on) stops here, before any PR-4 work, per the explicit instruction
that produced it.

**Unresolved PR-7 trigger/GUC design issues — approved on paper, not yet built, two specific findings
still open in the specification text itself:**

`CatalogComponentOverride` (spec §6.1) is the mechanism by which an installed rack/equipment instance can
diverge from its catalog template on specific fields, with the divergence tracked (not silently lost) when
the instance is later migrated to a different catalog revision. Its trigger design was reviewed and marked
"approved and authoritative" in the plan (§1.3a) — but as of this document, **zero code exists for it**: no
migration, no ORM model beyond a docstring forward-reference, no trigger function deployed. PR-1's own pull
request body ([#14](https://github.com/AhmedMahmoud2222/DCIM/pull/14)) recorded two specific carry-forward
findings against this still-unbuilt design that remain open in the specification text today:

1. **A `NULL`-comparison bug in the proposed trigger.** The specification's
   `fn_validate_catalog_component_override()` (design doc §6.1, around the pin-match check) reads:
   ```sql
   IF v_pinned_legacy_id NOT IN (v_bridge_rack, v_bridge_equipment) THEN
   ```
   Since exactly one of `v_bridge_rack`/`v_bridge_equipment` is `NULL` for any given revision (a revision
   bridges to *either* a legacy rack model *or* a legacy equipment model, never both), and SQL's `NOT IN`
   against a list containing `NULL` evaluates to `NULL` rather than `TRUE`/`FALSE`, and PL/pgSQL's `IF`
   treats `NULL` as false — **this guard can silently fail to reject a mismatched pin.** This was confirmed
   empirically in an isolated `DO $$ ... $$` block against the live test database during PR-1's review, not
   merely inferred. The proposed fix, not yet applied to the specification: replace the check with
   `v_pinned_legacy_id IS DISTINCT FROM v_bridge_rack AND v_pinned_legacy_id IS DISTINCT FROM
   v_bridge_equipment`, plus a direct-SQL regression test that reproduces the bug against the current
   `NOT IN` predicate before confirming the fix. Every trigger function PR-1 actually shipped uses
   `IS DISTINCT FROM` already — this `NOT IN` pattern is unique to the still-unimplemented §6.1 design.
2. **The `SET LOCAL app.catalog_migration_context` flag is a convention, not a privilege boundary.** The
   design correctly uses `SET LOCAL` so the flag cannot leak across a pooled connection to an unrelated
   transaction (it is cleared at commit/rollback). But every part of this application shares the same
   `dcim_app` database credentials — there is no per-endpoint database role separation here — so **any**
   code path holding those ordinary application credentials can set that same GUC itself immediately before
   issuing an UPDATE, with no special grant required. It stops *accidental* misuse from a code path that
   doesn't know to set the flag; it does **not** stop a malicious or compromised caller already holding
   ordinary application credentials, unlike this codebase's own `dcim_retention_admin` role, which is
   genuine, grant-enforced privilege separation. This is flagged, not fixed, in PR-1's own review: either
   the specification's framing of what this mechanism guarantees needs correcting before PR-7 is built, or
   PR-7 needs to adopt real role-based separation if a stronger guarantee is actually required. Neither has
   happened yet.

Whoever picks up PR-7 should resolve both before writing the migration, not after.

## 5. What "3D layout" and "network path" mean here, precisely

To avoid the two most common overclaims for a feature like this:

- **3D layout** renders the same authoritative 2D spatial projection (`/spatial/rooms/{id}/view`) with a
  CSS perspective transform — it is a view, not a second inventory, and it introduces no new persisted
  geometry. Missing physical values get view-only rendering defaults that are never written back to the
  database.
- **Network path / trace** returns a modeled physical path through the recorded device/interface/connection
  graph — an inventory-correctness statement ("does the wiring diagram connect end to end"), never a
  network-layer reachability statement ("can a packet actually get there"). No ping, traceroute, or
  protocol probe backs this endpoint. There is no LLDP/CDP or SNMP-based topology *discovery* (topology is
  operator-entered, with collector observations kept separately and never silently promoted to operator
  authority), no VLAN/VRF modeling, no routing awareness, and no live utilization figure anywhere in this
  path.
- Nothing in this repository, on any branch, performs or claims CFD/thermal simulation or autonomous
  (self-initiated) infrastructure operations of any kind.
