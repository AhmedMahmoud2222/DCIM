# Phase 8 Architecture Clarification: Driver Framework vs. Persistent Vendor/Device/Metric Layers

This is a new, forward-looking clarification document. It does not edit, replace, or
supersede any historical Phase 8 report (`PHASE8_IMPLEMENTATION_REPORT.md`,
`PHASE8_GAP_ANALYSIS.md`, `PHASE8_TRACEABILITY_MATRIX.md`,
`PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`, `PHASE8_BLOCKING_CORRECTION_REPORT.md`,
`PHASE8_FINAL_CLOSURE_VALIDATION.md`) — those remain the unedited historical record.
This document settles a specific question Codex's independent review raised (finding
H3, `PHASE8_CODEX_INDEPENDENT_RED_TEAM.md` / `PHASE8_CODEX_FINDING_VERIFICATION.md`)
and states the disposition plainly for anyone reading Phase 8's documentation going
forward.

## The question

`ARCHITECTURE_REVIEW.md` §20 ("Integration Layering") documents a five-layer stack:

```
Protocol → Driver → VendorProfile → DeviceProfile → MetricMapping
```

`PHASE8_TRACEABILITY_MATRIX.md` row 5 names this exact chain as a requirement and
marks it "STATICALLY VERIFIED." Codex's independent review correctly observed that
only the first two layers (`Protocol`/`Driver`) exist as real, operational code
(`app/application/drivers/base.py`'s `ProtocolDriver` ABC, `drivers/__init__.py`'s
`DRIVER_REGISTRY` dispatch) — `VendorProfile`, `DeviceProfile`, and a *persistent*
`MetricMapping` table do not exist anywhere in the domain model or migrations. The
only `MetricMapping` in the codebase is a driver-local, in-memory dataclass in
`app/application/drivers/snmp.py`, never loaded by `run_polling_cycle`, never backed
by a database table.

Is this a defect Phase 8 must close before it can be considered done, or a
requirement that belongs to a later phase?

## The answer: **B — the driver framework, not the persistent profile/mapping layers**

Phase 8's own scope documentation already settles this, independent of anything this
clarification adds:

1. `ARCHITECTURE_REVIEW.md` §18's own diagram places this entire stack's *purpose*
   downstream of telemetry: `Integration → Collector → ProtocolDriver → Device →
   MetricMapping → TelemetryReading`. `DeviceProfileMetric` (§20) has a foreign key
   directly to `TelemetryMetric`. Both `TelemetryReading` and `TelemetryMetric` are
   Phase 9 concepts — they do not exist in the schema today, on any branch, and
   nothing in Phase 8 needs them to.
2. `PHASE8_GAP_ANALYSIS.md` §5 ("Explicitly Out of Scope for This Task, Per Master
   Prompt §17/§18") states directly: *"Full `TelemetryReading` pipeline, time-series
   aggregation, TimescaleDB, alarm engine, notification engine, predictive analytics,
   AI, CFD, 3D twin — architecture Phase 9+... ownership stays with the future
   Telemetry domain."* `VendorProfile`/`DeviceProfile`/persistent `MetricMapping`
   exist, per §20's own diagram, to seed and configure that same telemetry pipeline
   — they are part of what §5 already deferred, not a separate omission.
3. Phase 8's own code is already honest about this in its own docstrings — e.g.
   `app/application/drivers/snmp.py`'s `MetricMapping` dataclass: *"this phase does
   not implement a persistent catalog of them (that is squarely Phase 9's 'metric
   mapping' table, per the architecture's own... ownership chain)."*

Phase 8's actual, required deliverable for this layering requirement was the
**driver framework**: a `ProtocolDriver` interface (`connect`/`poll`/`disconnect`/
`normalize`) that every concrete driver implements identically, dispatched by a
registry keyed on protocol code, with **zero vendor-conditional branching anywhere
outside `app/application/drivers/`** — confirmed, and still true at the pre-MVP
consolidation HEAD (re-confirmed independently, see
`PRE_MVP_CONSOLIDATION_REPORT.md` §10). That is a real, complete, tested
architectural property. Persistent `VendorProfile`/`DeviceProfile`/`MetricMapping`
tables are a **future, additive** layer that plugs into this same framework without
requiring it to change — exactly as `ARCHITECTURE_REVIEW.md` §20 itself describes
("vendor defaults exist as data, and `GenericSNMPDriver`/etc. never contain an `if
vendor == "APC"` branch anywhere in code").

## What this means for `PHASE8_TRACEABILITY_MATRIX.md` row 5

That row's own verdict text — "STATICALLY VERIFIED — no vendor-conditional
branching anywhere in the codebase" — is, read literally, accurate and unchanged:
it verifies the *absence of vendor branching*, not the *existence of persistent
VendorProfile/DeviceProfile tables*. This clarification exists because the
requirement's own name (copied verbatim from §20's five-layer diagram) can read, out
of context, as claiming the full stack operationally exists. It does not, and Phase 8
never required it to. The historical matrix file is left exactly as originally
written; this document is the correction to how that row should be read going
forward.

## What this means for Phase 8B / DCIM MVP v0.1

Not implemented here, per this consolidation task's own explicit instruction not to
build the vendor ecosystem. Recorded for whoever scopes that work:

- The Mini-DCIM MVP vertical slice ("connect a real device, show minimal real
  telemetry") will need **at minimum** enough device/metric mapping to know which
  OIDs/REST-fields/ICMP-signal correspond to which normalized metric name for ONE
  real device being connected — it does not need the full `VendorProfile` →
  `DeviceProfile` → seed-and-override chain to do that; a per-`Integration`
  `MetricMapping` alone (no vendor/device-profile catalog yet) would suffice for a
  single-device MVP slice.
- The full `VendorProfile`/`DeviceProfile` catalog (reusable vendor defaults across
  many integrations) is properly a Phase 9-adjacent scale/reuse concern, not an MVP
  vertical-slice blocker.
- Nothing about the current driver framework (`ProtocolDriver`, `DRIVER_REGISTRY`,
  the three concrete drivers) needs to change to support this later work — the
  clarification above is the reason this consolidation task did not need to modify
  `app/application/drivers/` for H3.

## Disposition

**H3: Confirmed architecture/requirements-clarity gap in documentation wording, not
an implementation gap and not a security defect.** No production code change was
required or made for H3. This document is the correction; no historical report was
edited.
