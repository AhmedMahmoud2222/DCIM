# G9a catalog publication Decimal projection — DRAFT

Parent: Issue #128, owner authorization comment 6108192722. Reference baseline:
`main@b573aaf3b037b4076db200838cf9c6d657513853` (cloud source).

No schema changes, template binding, spatial-import unit-table replacement, existing
record rewrite, G4 edits, deployment or database profiling are part of this change.
All merges remain blocked until G4-R is merged and independently accepted and the
owner approves release. This draft does not authorize enabling the prototype.

## Behavior and gate

`CATALOG_DECIMAL_PUBLICATION_PROJECTION=false` is the default. New publications keep
the exact existing float multiplication and Python nearest/half-even projection;
conversion factors now come from the registry v1 conversion API rather than duplicate
float literals. 7.5 in remains 190 mm; 0.5 lb remains 0 kg in this compatibility mode.

For an isolated draft experiment, setting the gate true projects newly published
**equipment only** through Decimal registry conversion, then `ROUND_HALF_UP` to mm
and kg. Any positive equipment authored mass has a 1 kg minimum. Example outputs:
19 in → 483 mm, 7.5 in → 191 mm, 2.5 in → 64 mm, 0.5 lb → 1 kg, 1.1 lb → 1 kg,
1000.1 lb → 454 kg. The registry's existing eight-decimal quantization occurs before
the whole-unit projection. Nonfinite, negative, incompatible-unit or PostgreSQL
integer-overflow inputs return publish validation errors before bridge insertion.

**Rack publication remains legacy-compatible even when the gate is true.** The
current bridge assigns rack authored `weight_value` to `weight_capacity_kg`. It is
not safe to apply a positive mass floor or round that capacity upward. The opt-in
prototype intentionally excludes the entire rack projection until that semantic
decision is resolved. This restriction is tested.

Authored NUMERIC revision values and units are never changed by projection. This
does not improve the existing API's float input parsing or its NUMERIC(10,3) authored
column precision. Already published revisions are rejected by the existing lifecycle
guard before projection; no backfill or recalculation is performed. Returning the
gate to false affects future equipment publications only, not already created rows.

## Consumer inspection and safety evidence

This is source inspection, not a claim about production records or impact. Search
covered backend application/domain/API code and frontend source for published
`width_mm`, `depth_mm`, `weight_kg`, `weight_capacity_kg` and authored physical fields.

| Consumer | Use and consequence |
|---|---|
| `application/catalog_designer_service.py` publish bridge | Rack authored weight becomes legacy load capacity; equipment authored weight becomes equipment mass. Raising both is semantically wrong for capacity. |
| `api/v1/catalog.py`, `domain/catalog/models.py`, frontend types | Store and expose integer dimensions, equipment mass and rack capacity. No runtime mass-summing or rack weight-limit enforcement was found in this baseline. External API consumers are unknown. |
| `api/v1/spatial.py` | Rack dimensions and floor-standing equipment dimensions feed room spatial output; equipment height is derived from U. Changing equipment projection changes drawn footprints. |
| `application/spatial_import/boundary.py` | Rack dimensions enter a placement-time approved-room-boundary guard, including explicit exception handling. Underestimated outer dimensions can accept a real out-of-bounds footprint. |
| `api/v1/floor_plans.py` room boundary update | Existing rack footprints are checked against a changed room outline. Rounding can change outside-room diagnostics. |
| `application/spatial_import/service.py`, `reconcile.py` | Rack dimensions enter footprint matching scores, rotations and intersection-over-union reconciliation. Changing projections can change candidate associations. |
| `application/spatial_validation.py` | Defines axis-aligned rack overlap/bounds advisory helpers. No call sites were found for these helpers in this baseline; do not claim they enforce fit. |
| `application/thermal/environment.py`, `thermal/capacity.py` | Rack width/depth determine centers, inferred sensor positions and zone membership. Boundary cases may change thermal grouping. |
| frontend `floor-plans/RoomSpatialCanvas.tsx`, `spatialMath.ts`, `ImportReviewCanvas.tsx`, `spatial3d/layout3dMath.ts` | Published dimensions draw and scale rack/equipment footprints and 3D geometry; rounding changes visible clearances. |
| Bulk import catalog templates/commit/validation | Author revision physical fields; publication still enters the same bridge. They are not a separate published-unit conversion path. |

The inventory instantiation and rack-placement checks were inspected for physical
references. No equipment-width/depth-versus-rack fit validator or published-weight
capacity check was found. Existing U-space enforcement concerns height_u and is not
changed. The separate spatial import conversion table remains out of scope.

Nearest rounding, whether half-even or half-up, can underestimate outer dimensions
by nearly 0.5 mm. Flooring outer width/depth is not conservative for collision or
room-boundary guards. A conservative policy would normally ceil consumed outer
dimensions and equipment load and floor available capacity/clearance; selecting
that policy would change more existing assumptions and requires explicit approval.
Likewise a 1 kg floor is not a general upward-load rounding rule: 1.1 kg remains
1 kg under the prototype, so the prototype is not safe as a conservative load bound.

## Explicit open owner decisions

1. Clarify whether rack authored weight means chassis mass or maximum supported
   payload. The existing bridge treats it as payload capacity. Correcting that
   relationship is a separate semantic change, not part of this prototype.
2. Select rounding by field role: nearest for display vs conservative outer
   dimensions, equipment mass and available rack capacity. Approve any feature
   enablement only after resolving these assumptions and external consumers.
3. Confirm whether positive sub-kilogram mass should use a 1 kg floor or a finer
   schema representation; the current integer schema cannot represent actual mass.
4. Approve release after G4 completion and independent review. Keep this PR DRAFT.

G10 provides the immutable v1 conversion snapshot and Decimal-context isolation.
This consumer uses `convert_value`'s historical default v1 so the draft is independently
reviewable on live main and remains compatible with G10's extended signature.

## Validation

Unit tests cover conversion vectors, null/zero/passthrough, ties, authored value
preservation, compatibility default, configuration default, rack capacity exclusion,
invalid units/nonfinite/negative values, integer limits, publish gate wiring,
published-record refusal and stopping before bridge insertion on invalid projection.
The session doubles open no database connection. A red collection run first failed
because the projection module did not exist; implementation then passed all tests.

Run from backend with the normal project environment:

```powershell
python -m pytest --noconftest tests/unit/test_catalog_physical_projection.py -q
python -m ruff check app/domain/catalog/physical_projection.py app/application/catalog_designer_service.py app/core/config.py tests/unit/test_catalog_physical_projection.py
```

The existing PostgreSQL catalog publication and catalog extraction-apply suites,
repository CI and deployment validation remain required before readiness; they are
not claimed to have been executed by these database-free tests.
