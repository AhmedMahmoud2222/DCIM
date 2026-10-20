# Issue #104: calibrated spatial digital twin

Document version: 1.0 (implementation complete, awaiting independent review)
Base: `main` at `12128d3ba896088ab3d3675cc4200f9bd4b9da27`. Migration: `0044_spatial_digital_twin` (single head, parent `0043_ops_correlation_itsm`).

## Decisions

1. **One spatial model.** `FloorPlan`, `SpatialLayer`, `SpatialObject`, `RackPlacement` and `EquipmentPlacement` stay the authority. Imported geometry is a `FloorPlanImportCandidate` until an operator accepts it. Accepted shapes keep `source = 'imported'` (existing contract) and carry a `provenance` record: job, candidate, source reference, SIR hash, calibration id, operator, time.
2. **PDF spatial import is not approved** and is rejected by content detection. DWG and binary DXF are rejected with explicit reasons.
3. **Authoritative position changes only by explicit operator flags.** Accepting a rack candidate records a drawn shape. `link_placement` links it to the rack's existing placement without changing coordinates. `apply_position` moves or places the rack (needs `rack:place` and the placement version). The system's suggested match is never enough: the operator must name the asset.
4. **Re-import is positional.** Candidates are compared with existing racks and already accepted shapes by position, footprint and orientation; labels are weak evidence. Shapes overlapping an accepted shape (IoU >= 0.6) are `duplicate` and refuse acceptance.
5. **Calibration is immutable lineage.** Each calibration is an insert-only row (DB trigger rejects UPDATE/DELETE). It can be replaced until the first shape is accepted under it, then it is locked for that revision.

## Importer architecture (`backend/app/application/spatial_import/`)

Upload -> content detection (`formats.py`, independent of filename; declared-vs-detected mismatch rejected) -> Celery task -> **sandboxed parser child** (`worker.py` via `runner.py`) -> sanitized intermediate representation (`sir.py`, re-validated by the parent) -> classification with evidence (`classify.py`) -> candidates -> calibration (`calibration.py`) -> reconciliation (`reconcile.py`) -> explicit accept.

* DXF: ASCII subset parser (`dxf_parser.py`): LINE, LWPOLYLINE, POLYLINE/VERTEX, CIRCLE, TEXT, MTEXT, INSERT flattening, layers, `$INSUNITS`. Everything else is counted and reported.
* VSDX: ZIP/XML parser (`vsdx_parser.py`): entry-count, name, encryption, declared and streamed size, ratio, relationship and content-type checks; defusedxml with DTD/entities/external forbidden; bounded XML depth/elements; bounded group depth and shape count. Only geometry, text and names are read; macros, ActiveX, OLE, embedded images are rejected or ignored.
* SVG and raster keep their hardened path and now flow through the same SIR/candidate pipeline.

## Parser isolation controls

The child reuses the existing extraction sandbox: scrubbed environment (no database, Redis, JWT or integration secrets), rlimits (CPU, address space), Python and seccomp network denial, Landlock with no write access and no access to `/proc/<parent>`, own process group killed on timeout/finish, wall-clock limit, bounded stdout, bounded input. Failure modes (timeout, crash, oversized output, forged SIR) become a `failed` job with a stable code; no partial candidates or SIR are stored, and the identical-upload key is cleared. If the sandbox is unavailable the import fails closed. Limits are in `limits.py`.

## Calibration and coordinates

Canonical coordinates stay integer millimetres, room-local origin, X right, Y down, rotation clockwise degrees. `u = (x - ox) * s`, `v = (y - oy) * s` (negated for Y-up sources), then rotated by `quadrants * 90` degrees. Methods: declared units, two points with a real distance (tolerances recorded), known room width/height (anisotropy above 2% refused), manual scale. Each stores scale, origin, axis, rotation, reference measurements, `error_bound_mm`, relative error and confidence (high <= 0.5%, medium <= 2%, else low). A short baseline (< 5% of the extent) is forced to low confidence. A Monte-Carlo test proves the stated bound covers the actual error.

## Engineering grid, 2D and 3D

The grid, rulers, origin marker, scale bar and snap interval are editor behaviour over canonical mm; nothing grid-related is stored. 2D draws racks at catalog width x depth and orientation, equipment at catalog footprint, the approved boundary, walls and columns. 3D uses one scale for room, footprint and height (rack height = U x 44.45 mm). A rack without a position or footprint is **not drawn**; it is listed. Equipment with a position but incomplete dimensions is a visibly non-scale pin. `layout_state` is `validated` only with an active plan, calibration, boundary and complete racks.

## Overlays (`GET /spatial/rooms/{id}/overlays`)

Power (protection-device and feed-impact aware, A/B redundancy), network (authoritative cables only; unconfirmed neighbours counted separately), environment (latest telemetry with measured/stale/missing; no estimation). Items use the same ManagedAsset ids. Each kind needs its own permission; a denied kind is a 403. Cooling/CFD (#105) is not implemented.

## Authorization

`floor_plan:*`, `spatial:read` are not site-scope-aware, so a site-restricted user has no effective permission and every floor-plan, import, candidate, diagnostics, source-geometry, calibration, boundary, view and overlay route returns 403 with no hidden identifiers (tested). Direct-ID access is therefore closed rather than filtered.

## Concurrency

Calibration and boundary writes use the floor plan `If-Match` version; candidate edit/accept/reject use the candidate version. Accept locks floor plan -> candidate -> placement. Placement versions are now monotonic per rack (a restart at 1 allowed a stale token to match the new row after a concurrent move: found by the accept-vs-move race test and fixed). Tests cover accept vs move, two candidates claiming one rack, overlapping candidates, accept vs recalibration, concurrent identical uploads and concurrent edits.

## Known limitations

* Re-import into a calibrated plan assumes the same source frame; a shifted re-export needs a new revision.
* Split/merge of candidates is not supported (candidate -> object is 1:1). Undo is per-candidate (last 20 steps).
* Floor-standing equipment is linked, not placed, by import; place it via the equipment API first.
* Arcs and bulges are approximated by chords and flagged; HATCH, SPLINE, DIMENSION, 3D entities are ignored with warnings; Visio master-inherited geometry is skipped with a warning.
* Celery carries the upload as base64 (20 MB cap); a larger ceiling needs object storage.
* The parser sandbox needs Landlock (kernel >= 5.13) and seccomp in the worker container.
