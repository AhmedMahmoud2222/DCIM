# Issue #150 — PDU catalog lifecycle/designer: architecture decision record

Status: documents the design **already present on `main` @ 9d85ef8**. No runtime code changed by this document.

## Decision

`pdu` is a **distinct supported catalog category** (not a constrained equipment subtype). A published PDU
revision instantiates the existing specialized runtime model: `ManagedAsset(asset_type='pdu')` + `PDU`
subtype + one `PowerNode`/`PDUOutlet` pair per outlet template. No second power graph exists; power links
remain `PowerConnection` rows.

Rationale: the runtime PDU already has its own subtype, outlet FK and power-graph identity
(`app/domain/power/models.py`). Recasting it as equipment would duplicate that model and orphan inventory.

## Canonical mapping

| Catalog (designer side) | Runtime |
|---|---|
| `CatalogModel.category='pdu'` | `ManagedAsset.asset_type='pdu'` |
| `CatalogModelRevision.pdu_input_voltage_v` / `pdu_input_current_a` | `PDU.input_voltage` / `PDU.rated_current_a` (snapshot copy) |
| `PDUOutletTemplate` rows | `PDUOutlet` + `PowerNode` rows; `PDU.outlet_count` |
| `pdu_phase`, `mounting_orientation`, `pdu_plug_type`, `pdu_rated_capacity_kw`, monitoring protocol/endpoint | catalog-only (no runtime columns; deliberately not invented) |
| no legacy bridge | `legacy_*_revision_id` must stay NULL for PDU (enforced by trigger `fn_guard_catalog_model_revision_lifecycle`, migration 0055) |

## Backward compatibility

- Migrations 0055/0056 are additive (new nullable columns, new `pdu_outlet_template` table, trigger branch for `pdu`); rack/equipment trigger behavior is unchanged. No data migration, no destructive step.
- Existing PDUs, `PDUOutlet` FKs, power links and telemetry are untouched. Instantiation is a snapshot, so later revisions never mutate existing PDUs.
- Rollback: downgrade 0056 → 0055 → previous; safe only while no `pdu` catalog models exist (otherwise delete them first).

## Where it lives

- API: `app/api/v1/catalog_designer.py` (category allow-list, `/pdu` draft PATCH, publish), `app/api/v1/power.py` (`POST /pdus/instantiate`, PDU placement).
- Services: `app/application/pdu_catalog/` (draft + pure validators), `catalog_designer_service.py` (publish), `pdu_instantiation_service.py`.
- Frontend: `frontend/src/features/catalog-designer/` (`PduSpecPanel`, `PduInstantiatePanel`), `frontend/e2e/pdu-catalog.spec.ts`.
- Tests: `backend/tests/api/test_pdu_catalog_draft.py`, `test_pdu_catalog_publish.py`, `test_pdu_instantiation.py`, `test_pdu_placement.py`.
