# Phase 10A — Asset Catalog Designer: Implementation Plan

**Status:** Planning only. No product code, migrations, or UI are implemented by this document or this
branch. This plan sequences work described in the approved specification into reviewable PRs; it does not
alter that specification's architecture, schema, or endpoint contracts except where explicitly flagged
below as a finding for review.

## 0. Verification record

| Item | Value |
|---|---|
| Repository | `AhmedMahmoud2222/DCIM` |
| Design branch | `claude/phase10a-asset-catalog-design` |
| Design branch HEAD (verified via `git rev-parse origin/claude/phase10a-asset-catalog-design`) | `13165d3aa595272234dae7edd3587c6f997da468` |
| Ancestry (`git merge-base --is-ancestor 224f122d... 13165d3...`) | Confirmed — `224f122d950ee2631180940b6b8a429f1379b266` is an ancestor of `13165d3` |
| Base → design-branch-HEAD diff (`git diff --no-renames --name-status`) | **Document-only**: exactly one file, `docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md` (added, 1342 lines). No product code, migration, or config file differs from `224f122d950ee2631180940b6b8a429f1379b266`. |
| Specification path (confirmed present at design-branch HEAD) | `docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md` |
| Specification read | In full, 1342 lines, §0–§19 |
| Correction commit under review | `13165d3aa595272234dae7edd3587c6f997da468` — "docs: correct and complete Phase 10A catalog design," a same-file rewrite of the original `PHASE10A_ASSET_CATALOG_DESIGN.md` (commit `9a65cc8`, now deleted) moved to its requested path with 109 insertions / 45 deletions of correction content (lifecycle locking, `CatalogComponentOverride`, delivery plan, acceptance criteria, risk table) |
| Alembic current head (walked `down_revision` chain across `backend/migrations/versions/`, re-verified fresh on this branch) | `0016_network_runtime_defaults` — unchanged from the design's own verification record |
| Planning branch | `claude/phase10a-asset-catalog-designer-plan`, created from `13165d3aa595272234dae7edd3587c6f997da468` exactly (`git checkout -b ... 13165d3...`) |

---

## 1. Specification cross-check against the current repository

Every schema, endpoint, migration-ordering, inheritance, and test claim in the specification was checked
directly against the repository at the design branch's HEAD (which carries no code changes beyond
`224f122`, so this is equivalent to checking against `224f122` itself). Findings are grouped by severity.
None of these are treated as license to change the approved architecture — each is either a confirmation,
a concrete gap the implementation must additionally cover, or a decision flagged back for review before
work starts.

### 1.1 Confirmed accurate (no action needed beyond noting verification)

| Spec claim | Verification |
|---|---|
| `NetworkInterface` has no `media_type`/`connector_type` columns (§6.1's correction) | Confirmed by reading `app/domain/network/models.py:32-58` — columns are `device_id, name, interface_type, description, mac_address, role, admin_status, oper_status, speed_mbps, duplex, mtu, native_vlan, ip_address, source, last_observed_at`. No `media_type`/`connector_type`. |
| Legacy `catalog.py` endpoints still gate on `rack:manage`/`equipment:manage`, not a dedicated `catalog:*` code (§9.1) | Confirmed — `app/api/v1/catalog.py:59,86,156,185`. |
| `RacksPage.tsx:104-106` inline-mint gap comment (§1.1, §11.3) | Confirmed verbatim present. |
| Alembic head is `0016_network_runtime_defaults` | Re-walked the `down_revision` chain fresh on this branch; confirmed. |
| `IntegrationMetricMapping` has no `metric_template_id` yet (§6.1 proposes adding it) | Confirmed — `app/domain/telemetry/models.py:33-53`; columns are `integration_id, managed_asset_id, source_identifier, canonical_metric, unit, scale, label`. |
| `app/core/config.py`'s `Settings` is a plain `pydantic-settings BaseSettings` with `env_file` — adding `catalog_image_storage_dir` is a same-shape addition | Confirmed, `app/core/config.py:8-13`. |
| `backend/tests/api/test_security.py`, `backend/tests/unit/test_svg_sanitizer.py`, `backend/tests/integration/test_db_constraints.py`, `backend/tests/integration/test_outbox.py` exist as citable precedent | Confirmed present. |
| `trg_managed_asset_replacement_acyclic` precedent for hand-written raw-SQL triggers | Confirmed, `backend/migrations/versions/0003_correction_idempotency_and_replacement_integrity.py`. |

### 1.2 Missing dependencies — concrete, not called out in the specification

These are real implementation prerequisites the specification does not mention. None contradict the
architecture; they are infrastructure the architecture depends on that must be scheduled explicitly or a
PR will fail to work at all.

1. **`backend/app/db/models.py` is the single import point that registers every ORM model on
   `Base.metadata` before Alembic autogenerate or `create_all` runs** (its own module docstring says so
   verbatim). Every new table needs an entry here, added in the **same PR that creates its migration** —
   not all bundled into PR-1. Corrected in this update: an earlier version of this list named all ten new
   tables together and said "every schema PR below includes a diff," which read as though
   `CatalogComponentOverride` and `CatalogImportJob` belonged to PR-1's scope; they do not, and PR-1's own
   migration list (§3.1) never created them. The actual split, matching where each table's migration
   actually lands:
   - **PR-1** registers the eight §4 tables (`Manufacturer`, `CatalogModel`, `CatalogModelRevision`,
     `NetworkPortTemplate`, `PowerSupplyTemplate`, `CatalogGraphic`, `CatalogGraphicMarker`,
     `MonitoringMetricTemplate`).
   - **PR-6** registers `CatalogImportJob` (§13.2).
   - **PR-7** registers `CatalogComponentOverride` (§6.1, approved design in §1.3a below).

   The specification never mentions this file at all. **Action:** PR-1, PR-6, and PR-7 each carry their
   own explicit `backend/app/db/models.py` checklist item in their own Backend files list below — not a
   single blanket instruction covering all three.
2. **`backend/app/api/v1/router.py` is the single composition point for every router** (`api_router.include_router(...)`, one line per module). The specification names the new router file
   (`app/api/v1/catalog_designer.py`, §10) but never mentions registering it here. **Action:** the PR that
   introduces the new router includes this one-line registration explicitly.
3. **`app/application/rbac.py`'s `DEFAULT_ROLE_PERMISSIONS` dict is the human-readable source the seed
   migration (`0002_audit_partitions_retention_and_rbac_seed.py`) imports from at migration-run time**, and
   its own module docstring calls it *"the single source of truth both the seed migration and this
   module's own `require_permission()` dependency are checked against."* Read literally that overstates
   it — `require_permission()`'s actual runtime check (`get_auth_context`) queries the `permission`/
   `role_permission`/`role`/`role_assignment` tables directly, never this Python dict — but the dict is
   still the only place a human (or a future migration/test) reads to know "what does Administrator hold
   today." A new catalog-RBAC seed migration must insert new `permission`/`role_permission` rows directly
   (it does not need to re-run migration `0002`'s dict-derived bulk-insert logic), but **this plan requires
   `DEFAULT_ROLE_PERMISSIONS` to be updated in the same PR** so the dict does not silently drift from the
   database it claims to describe — the specification's migration-sequencing section (§12) never mentions
   this file.
4. **Frontend list-endpoint availability at the point the legacy-endpoint closure (§9.1, §11.3) ships.**
   The specification's §11.3 says the inline-mint replacement in `RacksPage.tsx`/`EquipmentPage.tsx` should
   call `GET /catalog/models`-style **new** endpoints — but per this plan's own sequencing (§3 below), the
   new endpoints do not exist yet at the point legacy-endpoint closure must land (closure has to land
   *before* the new publish-capable surface is exposed, not after). **Resolution proposed by this plan
   (flagged for review, not a silent change):** the inline-mint replacement initially sources its picker
   from the **already-existing** `GET /rack-models`, `GET /rack-models/{id}/revisions`,
   `GET /equipment-models`, `GET /equipment-models/{id}/revisions` endpoints (already `*:read`-gated,
   already broadly granted), which is sufficient to stop non-admins from minting *new* catalog rows while
   still letting them select from whatever legacy catalog rows already exist. Once the new
   `GET /catalog/models` family ships, a follow-up change repoints the picker — tracked as an explicit task
   in PR-4, not assumed automatic.

### 1.3 Decisions flagged for review — not silently resolved

1. **`CatalogComponentOverride` (§6.1) — RESOLVED this update, see §1.3a below.** Was the one table in the
   specification without a concrete column list; is now an approved design, subject to four requirements
   recorded verbatim in §1.3a, not silently resolved by this plan on its own authority.
2. **§12 item 4's stated rationale for deferring the legacy bridge columns to their own migration is
   slightly broader than necessary, though the sequencing itself is sound.** `legacy_rack_model_revision_id`/
   `legacy_equipment_model_revision_id` (the columns **on** `catalog_model_revision`, pointing **at** the
   pre-existing `rack_model_revision`/`equipment_model_revision` tables) could technically be added in the
   same migration that creates `catalog_model_revision` (migration 1 / PR-1 below), since
   `rack_model_revision`/`equipment_model_revision` already exist today at head `0016` — the "must already
   exist" constraint only genuinely applies to the **reverse** column,
   `bridged_from_catalog_revision_id` **on** `rack_model_revision`/`equipment_model_revision`, which
   cannot exist until *after* `catalog_model_revision` is created. This plan keeps the specification's
   choice to land both halves of the bidirectional bridge together in one later migration anyway (§3.1's
   migration 4) — it is the more reviewable and more easily-reverted unit even though not strictly
   required by table-existence ordering — but flags the stated rationale as imprecise so a reviewer does
   not mistake it for a hard technical constraint.
3. **`CatalogModel.category` intentionally omits `'cable'`** (present in `ManagedAsset.ASSET_TYPES` but
   excluded here since a cable has no revisioned model). Confirmed intentional and consistent with the
   design's own reasoning — noted here only so the omission reads as a decision, not an oversight, when a
   reviewer diffs the CHECK list against `ASSET_TYPES`.
4. **Cross-revision marker validation trigger (§4.5's prose: "the marker trigger resolves its parent
   revision through `catalog_graphic_id`, locks that revision, and rejects a target port/PSU belonging to a
   different revision") has no SQL body in the specification**, unlike the immutability trigger in §5.4
   which is given in full. §12 item 3 acknowledges this table needs its own trigger ("parent-resolution and
   same-revision target triggers") but the exact function is left to implementation. This is normal for a
   design-level document and not treated as a defect, but PR-1/PR-5 (wherever `catalog_graphic_marker`'s
   migration lands, per §3 below) must write this trigger from the stated *behavior*, and its SQL should be
   reviewed with the same scrutiny as §5.4's trigger before merge, since it is new, unwritten logic.

### 1.3a `CatalogComponentOverride` — approved design, now authoritative in the specification

**Resolved and moved.** The full schema, field allowlist, pin/component-existence trigger, and atomic
per-asset transaction are no longer duplicated here — they are now the specification's own §6.1, which
this plan implements exactly as written there, not as a separate or competing source. This subsection
records only what changed and why, so the review trail stays intact; PR-7 (§3.7) cites spec §6.1 directly
for every implementation detail.

**What changed from this plan's earlier version, in the move:** the original design here let *any* write
setting `status = 'orphaned'` skip the pin/component-existence check unconditionally — reviewed and closed.
An ordinary caller could otherwise have inserted a brand-new row, pre-marked `orphaned`, referencing any
`catalog_model_revision_id`/`stable_key` combination whatsoever, never validated against anything. Spec
§6.1's corrected trigger closes this: a new row must always be created `active` (an `INSERT` requesting any
other status is rejected outright), the active → orphaned transition is permitted only inside a catalog
migration's own transaction (gated by a transaction-local `SET LOCAL app.catalog_migration_context = 'on'`
flag the migration service sets immediately after taking its per-asset lock — never leaks across a pooled
connection, since `SET LOCAL` is cleared at commit/rollback), and an already-`orphaned` row is immutable
thereafter (delete/reset only). Spec §6.1 also settles who may call which path: creating or resetting an
override is gated by the asset's own `equipment:manage`/`rack:manage` permission (ordinary asset editing,
no Administrator requirement) — the orphaning transition specifically is reachable **only** from the
migration service, itself gated by `catalog:migrate` plus Administrator role membership (§5/§9.1), and is
never a directly requestable operation through any public request body.

None of the above 1.3 items — 2, 3, and 4 — are contradictions that block planning; they are exactly the
kind of "needs review before implementation" items the specification's own thoroughness elsewhere makes
conspicuous by their absence. This plan proceeds using the item 2–4 proposals as **placeholders**, each
marked in the PR breakdown as flagged for review, not silently treated as approved. Item 1
(`CatalogComponentOverride`) is no longer a placeholder and no longer lives here — it is spec §6.1.

---

## 2. Signing workflow finding — `13165d3` is genuinely unsigned, not just locally unverifiable

**This repository's verified signing workflow, confirmed by direct testing in this session:**

- This session's global git config (`/root/.gitconfig`) has `commit.gpgsign=true`, `gpg.format=ssh`,
  `user.signingkey=/home/claude/.ssh/commit_signing_key.pub`, `gpg.ssh.program=/tmp/code-sign` (a symlink to
  `/opt/env-runner/environment-manager`, the harness's own binary).
- A disposable test commit made in this session (`git init` in a scratch directory, immediately deleted
  after inspection — never touched the real repository) produced a commit object with a real, well-formed
  `gpgsig -----BEGIN SSH SIGNATURE-----...` block. **Signing is functionally active in this session.**
- `git log --show-signature`'s local `"error: gpg.ssh.allowedSignersFile needs to be configured"` /
  `"No signature"` messages on a commit that *does* carry a `gpgsig` block are a **local verification
  limitation only** (this sandboxed container has no `allowedSignersFile` to check the SSH signature
  against) — not evidence the commit is unsigned. Verification of these signatures happens GitHub-side
  against the public key GitHub already has for the committing identity.
- Direct proof this mechanism was already working earlier in this same design work: `git cat-file commit
  9a65cc8ff024e25e41ac2244db988ad769af3274` (the original design-doc commit, author `Claude
  <noreply@anthropic.com>`) **does** contain a `gpgsig` SSH signature block — confirmed by reading the raw
  object, not by trusting local verification output.

**`13165d3aa595272234dae7edd3587c6f997da468` (`docs: correct and complete Phase 10A catalog design`) has
no `gpgsig` header at all** — confirmed by `git cat-file commit 13165d3...`, which shows `tree` / `parent` /
`author` / `committer` / message with nothing between `committer` and the blank line before the message.
This is not a "can't verify locally" situation; the commit object itself carries no signature for anyone,
anywhere, to verify. Its author/committer identity is also different from the signed commits above —
`AhmedMahmoud2222 <d6g6f7kb6r@privaterelay.appleid.com>` — matching the repository owner's own identity, as
confirmed by `mcp__github__get_commit` (GitHub API `author.login: "AhmedMahmoud2222"`,
`committer.login: "AhmedMahmoud2222"`), rather than the `claude`/Claude-Code-Remote identity
(`author.login: "claude"`) that produced the signed `9a65cc8`. This commit was made through a path that
does not carry this session's harness-managed signer — most plausibly a different tool or environment than
this Claude Code Remote session.

**Remedy path, for the record, and why this plan does not take it:** the only way to turn `13165d3` into a
signed commit would be to replace it with a re-signed equivalent — either `git commit --amend` from a
session with working signing, or an interactive rebase that re-creates it — both of which **rewrite
published history on a branch already pushed to `origin`**, exactly the action the task explicitly forbids
without the user's explicit authorization.

**Decision recorded (this update): `13165d3` is accepted as permanent, documentation-only unsigned
history.** It is not amended, rebased, or force-pushed over — by this update or any future one, absent a
separate, explicit instruction to do so. This is scoped narrowly to this one commit; it is not a general
relaxation of the signing expectation — every other commit on this branch, including the one recording this
decision, continues to use the repository's verified signing workflow, re-confirmed signed before push at
the end of this document. Concretely, this plan:

- Leaves `13165d3` untouched — no amend, no rebase, no force-push.
- Does not describe it, or any future re-derivation of its content, as signed or verified anywhere in this
  plan or its own commits.
- Continues building the planning branch as ordinary additive commits on top of `13165d3` (a normal,
  non-rewriting branch-from-tip operation).

---

## 3. PR sequence

Eight PRs, each independently reviewable and mergeable in the listed order. The mandatory gate the task
asked for explicitly — **administrator-only enforcement and closure of existing catalog-authoring paths
before exposing the new publishing workflow** — is enforced by construction: PR-2 lands the new `catalog:*`
permissions and closes every existing non-admin catalog-authoring path *before* PR-3 introduces the first
endpoint capable of publishing a new catalog definition. No PR after PR-2 re-opens a non-admin
catalog-authoring path.

| PR | Title | Depends on | Exposes new admin-mutation capability? |
|---|---|---|---|
| 1 | Schema and DB guards | — | No — inert tables, no routes |
| 2 | RBAC seed and existing-path closure | 1 | No new capability; **removes** a non-admin one |
| 3 | Catalog lifecycle backend (draft → publish → retire) | 2 | **Yes — first gate crossing, admin-only by construction** |
| 4 | Core administrator UI | 3 | No new backend capability; frontend only |
| 5 | Graphics and markers | 1, 3, 4 | Yes — admin-only, same gate already in place |
| 6 | Portable JSON import/export | 3 | Yes — admin-only, same gate already in place |
| 7 | Installed-asset migration and instance provenance | 1, 3, 4 | Yes — admin-only (`catalog:migrate`), same gate |
| 8 | Monitoring-template seeding, regression, and documentation | 3, 5, 6, 7 | No new capability; verification and docs |

Phase 10B (Network Operations) and Phase 10C (Spatial Digital Twin) are out of scope for every PR above —
none touches `NetworkConnection`/`PowerConnection` topology editing, 3D/`SpatialObject` geometry, collector
execution, or discovery automation, matching §15 of the specification exactly. No PR in this plan opens
any of that surface.

### 3.1 PR-1 — Schema and DB guards

**Scope:** every new table from §4, purely additive, no application code reads or writes them yet.

**Migrations** (chaining from `0016_network_runtime_defaults`, per §12 — filenames illustrative per the
specification's own footnote, to be renumbered against whatever the actual head is when work starts):

- `0017_catalog_manufacturer_and_model` — `manufacturer`, `catalog_model`, `catalog_model_revision`
  (**without** the two `legacy_*_revision_id` bridge columns — see §1.3 item 2 above; they land in this
  same PR's migration 4 below, not split into a separate PR, to keep the "does this table exist yet"
  question simple across the whole PR), `fn_reject_write_on_non_draft_revision()`, the narrower
  `catalog_model_revision` lifecycle-transition trigger, and the `CatalogModel` identity-lock triggers
  (§5.4).
- `0018_catalog_component_templates` — `network_port_template`, `power_supply_template`,
  `monitoring_metric_template` + their `BEFORE INSERT/UPDATE/DELETE` triggers (reusing
  `fn_reject_write_on_non_draft_revision()`).
- `0019_catalog_graphics` — `catalog_graphic`, `catalog_graphic_marker` + the immutability trigger reuse
  **and** the new cross-revision marker-validation trigger flagged in §1.3 item 4 (written and reviewed as
  part of this PR, not deferred).
- `0020_catalog_legacy_bridge` — `bridged_from_catalog_revision_id` (nullable, unique) added to
  `rack_model_revision` and `equipment_model_revision`; `legacy_rack_model_revision_id`/
  `legacy_equipment_model_revision_id` + their XOR `CHECK` added to `catalog_model_revision`; the
  "reject direct mutation of a bridged legacy row" trigger on both legacy tables (§5.4).

**Backend files:**
- `backend/app/domain/catalog/designer_models.py` (new — keeps the existing, small
  `backend/app/domain/catalog/models.py` untouched rather than growing it to ~9 additional classes;
  both live under the same `catalog` domain package).
- `backend/app/domain/catalog/models.py` — add the `bridged_from_catalog_revision_id` mapped column to
  `RackModelRevision`/`EquipmentModelRevision` (the only edit to this file in the whole plan).
- `backend/app/db/models.py` — import every new class (finding §1.2 item 1).
- `backend/migrations/versions/0017_...py` … `0020_...py` — four migrations above, each with a real
  `downgrade()` (drops tables/columns/functions in reverse order).

**No API, no frontend, no RBAC changes in this PR.**

**Tests:**
- `backend/tests/integration/test_catalog_designer_schema.py` (new, matching
  `test_db_constraints.py`'s style): every `CHECK`/`UNIQUE` constraint in §4 exercised directly with raw
  SQL/ORM inserts; the immutability trigger rejects a raw `UPDATE`/`INSERT`/`DELETE` against a
  `published`/`retired` revision's children; the identity-lock trigger rejects a `CatalogModel` identity
  edit once any revision is published; the legacy-bridge XOR `CHECK` rejects both columns populated at
  once; the cross-revision marker trigger rejects a marker pointing at a component from a different
  revision.
- `alembic upgrade head` / `alembic downgrade -1` round-trip for each new migration (existing repo
  convention, README.md's own documented test-setup step).

**Acceptance:** `alembic upgrade head` succeeds against a fresh `dcim_test` database; every existing test
in `backend/tests/` still passes unmodified (this PR touches no existing runtime code path); the four new
trigger behaviors are demonstrated failing correctly in the new integration test file.

### 3.2 PR-2 — RBAC seed and existing-path closure (the mandatory gate)

**Scope:** create the `catalog:*` permission family and close every non-admin catalog-authoring path
*before* any new catalog-mutation endpoint exists. No new catalog capability is exposed by this PR — it
only adds permissions (inert until PR-3 depends on them) and **removes** the Engineer/DCIM-Manager ability
to mint catalog rows today.

**Migration:**
- `0021_catalog_rbac_seed` — `sa.bulk_insert` seven new `permission` rows (`catalog:read`,
  `catalog:read_draft`, `catalog:manage`, `catalog:publish`, `catalog:retire`, `catalog:import`,
  `catalog:migrate`) and their `role_permission` rows: `catalog:read` to
  all five seeded roles (Administrator, DCIM Manager, Engineer, Operator, Viewer — all five already hold
  `rack:read`/`equipment:read`, confirmed in §1.1); every other `catalog:*` code to Administrator only.
  Modeled directly on `0002_audit_partitions_retention_and_rbac_seed.py`'s own `sa.table`/`op.bulk_insert`
  pattern, looking up existing role ids by name (roles already exist; this migration only adds permissions
  and their grants, it does not re-create roles).

**Backend files:**
- `backend/app/application/rbac.py` — add the seven `catalog:*` codes to `DEFAULT_ROLE_PERMISSIONS`
  for every role listed above (finding §1.2 item 3 — keeps the dict truthful, even though the migration
  itself does not re-derive from it); add `AuthContext.role_names`/`has_role()`, the corrected
  `get_auth_context()` query, and `require_catalog_administrator(code)` — spec §9.1's exact design (§5
  above), additive and non-breaking to every existing `require_permission()` call site.
- `backend/app/api/v1/catalog.py` — change all four mutating dependencies from
  `require_permission("rack:manage")`/`require_permission("equipment:manage")` to
  `require_catalog_administrator("catalog:manage")` — permission **and** Administrator role membership,
  not permission alone (§5); the two list (`read`) endpoints keep their existing `rack:read`/
  `equipment:read` gate (read access stays broad, per product principle 9 — only *authoring* is
  tightened). Update this file's own module docstring to note the tightened requirement.

**Frontend files:**
- `frontend/src/features/racks/RacksPage.tsx` — remove the inline manufacturer/model-name/height/width/
  depth "new rack model" sub-form; replace with a `<select>` sourced from the existing
  `GET /rack-models` + `GET /rack-models/{id}/revisions` (already-exposed, already `rack:read`-gated)
  endpoints (per the resolution in §1.2 item 4).
- `frontend/src/features/equipment/EquipmentPage.tsx` — identical treatment for the equipment-model
  inline mint.
- `frontend/src/features/racks/api.ts`, `frontend/src/features/equipment/api.ts` — add
  `listRackModelRevisionsFor(modelId)`-style read helpers if not already present in a directly reusable
  shape (both files already export `listEquipmentModels`/`listEquipmentModelRevisions`/Rack equivalents
  per the earlier research — confirm reuse before adding new functions).

**Tests:**
- `backend/tests/api/test_catalog_rbac_closure.py` (new): a user holding only `Engineer`'s permission set
  gets 403 from every mutating `catalog.py` endpoint (the concrete regression test specification §9.10
  calls for); the four dual-check regression tests from §5 (synthetic non-Administrator role holding only
  `catalog:manage` → 403; `Administrator` with `catalog:manage` revoked → 403; `Administrator` holding
  `catalog:manage` → success; reads unaffected) run against these same four legacy endpoints, not only
  against PR-3's new router; the two list (`read`) endpoints remain reachable by every role that held
  `rack:read`/`equipment:read` before this PR (no read regression).
- `frontend/src/features/racks/RacksPage.test.tsx` / `EquipmentPage.test.tsx` (extend existing, or add):
  the inline-mint form no longer renders; the model/revision picker renders and drives `createRack`/
  `createEquipment` with a selected `model_revision_id` instead of newly-minted identity fields.

**Acceptance (this is the explicit rollout gate the task required):** at the end of this PR, no
authenticated user lacking **both** `catalog:manage` **and** Administrator role membership can create a new
`RackModel`/`RackModelRevision`/`EquipmentModel`/`EquipmentModelRevision` row through any reachable path,
frontend or API — verified by the new `test_catalog_rbac_closure.py` suite, including its
non-Administrator-custom-role and revoked-permission cases (§5). This must be true and merged **before
PR-3 opens the first new catalog-mutation endpoint.**

### 3.3 PR-3 — Catalog lifecycle backend

**Scope:** the full typed manufacturer/model/draft/validate/publish/retire/clone/compare backend. Every
mutation endpoint depends on `require_catalog_administrator(code)` (§5, spec §9.1) — permission and
Administrator role membership, not permission alone; read endpoints (`catalog:read`/`catalog:read_draft`)
depend on plain `require_permission(code)`, PR-2 having already seeded and enforced both dependencies.

**Backend files:**
- `backend/app/api/v1/catalog_designer.py` (new router — §10's manufacturer, model, and revision-lifecycle
  endpoints only; graphics/markers go in PR-5, import/export in PR-6, migration in PR-7, all as additions
  to this same router file). Every `POST`/`PATCH`/`DELETE` handler's `Depends()` is
  `require_catalog_administrator("catalog:...")`; every `GET` handler's is `require_permission("catalog:...")`
  (or the conditional check §10 describes for `GET /catalog/revisions/{id}`).
- `backend/app/api/v1/router.py` — register the new router (finding §1.2 item 2).
- `backend/app/application/catalog_designer_service.py` (new — draft mutation, validation-rule
  evaluation per §5.2, publish-time legacy-bridge creation per §4.7, `write_audit_log`/`write_outbox_event`
  calls per §8's table, all under the parent-row-locking discipline §5.4 requires).

**Tests:**
- `backend/tests/api/test_catalog_designer_lifecycle.py`: create manufacturer → model → draft revision →
  edit → validate (both failing and passing cases from §5.2's rule list) → publish → verify immutability
  (409 on any further child write) → verify legacy bridge row created correctly, including the unit
  conversion case (inches/lb draft → mm/kg legacy row) → clone → retire (`reason` required, rejected
  without one) → `allow_installation_when_retired` toggle.
- `backend/tests/integration/test_catalog_designer_concurrency.py`: two concurrent draft edits race
  correctly via `If-Match`/`version`; concurrent publish attempts serialize correctly via the parent-row
  lock (§5.4) — one succeeds, the other sees a consistent, already-published state, never a corrupted
  partial publish.
- Full 403 matrix from §9.10 for every new endpoint in this PR, including the non-Administrator-custom-role
  and revoked-permission dual-check cases (§5) — not just the plain missing-permission case.

**Acceptance:** an administrator can create and publish a rack or equipment revision end-to-end via the
API, matching the first sentence of the specification's §18 acceptance criteria; no principal lacking
either `catalog:manage`-family permission or Administrator role membership can reach any mutating endpoint
in this router (build on PR-2's closure and its dual-check tests, not a re-test of the mechanism from
scratch — only new-router-specific coverage).

### 3.4 PR-4 — Core administrator UI

**Scope:** §11's bounded screens, `/admin/catalog/*` route tree, nav entry. No new backend capability.

**Frontend files:**
- `frontend/src/app/App.tsx` — new route tree under `/admin/catalog/*`.
- `frontend/src/components/layout/AppShell.tsx` — new **Admin** nav group (does not exist today,
  confirmed in the design's own research).
- `frontend/src/components/layout/RequireCatalogAdmin.tsx` (new, mirrors `ProtectedRoute.tsx`'s
  UX-convenience-only posture, gated on `useHasPermission("catalog:manage")`).
- `frontend/src/features/catalog-designer/` (new feature directory, matching the existing per-feature
  flat-file convention): `CatalogHomePage.tsx`, `ManufacturerDetailPage.tsx`, `ModelDetailPage.tsx`,
  `RevisionEditorPage.tsx` + its section components (`RevisionIdentitySection.tsx`,
  `RevisionPhysicalSection.tsx`, `RevisionElectricalSection.tsx`, `PowerSupplyTemplateEditor.tsx`,
  `NetworkPortTemplateEditor.tsx`, `MonitoringTemplateEditor.tsx` — graphics editor deferred to PR-5),
  `ValidationSummaryDialog.tsx` (reuses `components/ui/Dialog.tsx`), `RevisionCompareView.tsx`, `api.ts`,
  `types.ts` (extends `frontend/src/types/index.ts`'s existing Catalog section rather than duplicating it).

**Tests:** component tests per section (form round-trip, validation summary rendering) following the
existing `renderPage()`/`setSession()` idiom from `NetworkPage.test.tsx`; a route-level test that a
non-admin session never renders the Admin nav group or reaches `/admin/catalog/*` (UX-only, backend-tested
already in PR-2/3).

**Acceptance:** an administrator can complete the full create→edit→validate→publish→compare flow through
the UI without reading API docs; a non-admin session sees no path to any of it.

### 3.5 PR-5 — Graphics and markers

**Scope:** §7's storage infrastructure and endpoints, §11.1's marker editor.

**Backend files:**
- `backend/app/infrastructure/storage/catalog_image_storage.py` (new — `CatalogImageStorage` protocol +
  `LocalDiskCatalogImageStorage`).
- `backend/app/core/config.py` — add `catalog_image_storage_dir` setting.
- `backend/app/api/v1/catalog_designer.py` — add the graphics/marker endpoints from §10's table, every
  mutating one gated by `require_catalog_administrator("catalog:manage")` (§5); `GET .../file` gated by
  plain `require_permission("catalog:read")`.
- Reuses `app/application/svg_sanitizer.py`'s `validate_raster_image()` verbatim (import, not
  reimplementation) — no changes to that file.

**Frontend files:** `frontend/src/features/catalog-designer/GraphicsMarkerEditor.tsx` (canvas + the
keyboard-operable tabular fallback, §11.1) and its co-located test.

**Tests:** `backend/tests/unit/test_catalog_image_validation.py` mirroring `test_svg_sanitizer.py`'s
structure (oversized file, bad magic bytes, oversized/decompression-bomb-shaped dimensions, SVG rejected,
PNG/JPEG accepted); an integration test for replace-semantics (old graphic + its markers deleted
transactionally, port/PSU template rows survive unmarked); a frontend test that every marker is reachable
and placeable via the keyboard-only tabular form with no pointer events.

**Acceptance:** upload/replace/remove works end-to-end; SVG is rejected with a clear 422; marker placement
survives a revision clone (§5.5) with markers re-pointed at the cloned components, not the originals.

### 3.6 PR-6 — Portable JSON import/export

**Scope:** §13 in full.

**Backend files:**
- `backend/app/api/v1/catalog_designer.py` — import/export endpoints; preview/apply gated by
  `require_catalog_administrator("catalog:import")` (§5); export (`GET`) gated by plain
  `require_permission("catalog:read")`.
- `backend/app/application/catalog_import_export.py` (new — `CatalogImportDocumentV1` Pydantic tree,
  preview/apply logic, deterministic export ordering).
- `backend/migrations/versions/0022_catalog_import_job.py` — `catalog_import_job` (including the
  `canonical_document JSONB` column the correction commit added, §13.2).
- `backend/app/db/models.py` — register `CatalogImportJob` (finding §1.2 item 1, corrected).

**Tests:** `backend/tests/api/test_catalog_import_export.py` — oversized document (413), over-limit counts
(422), malformed JSON, duplicate-name conflict reporting (never silently merged), idempotent re-apply of an
already-applied job, round-trip export→import producing byte-identical re-export.

**Acceptance:** import never creates a published revision (always lands as draft, per §13.2); export never
contains binary image data or any credential-shaped field (verified by an explicit assertion over the
export schema, not just "no field named `password`").

### 3.7 PR-7 — Installed-asset migration and instance provenance

**No longer blocked:** `CatalogComponentOverride`'s design is approved and authoritative in specification
§6.1 (§1.3a) — this PR implements exactly what that section specifies: schema, allowlist, the
bypass-closed pin/component-existence trigger, and the atomic per-asset transaction step order. This
section states only PR-7-specific implementation detail (files, tests); the schema and trigger themselves
are not restated here — see spec §6.1.

**Scope:** §5.8/§5.9 (impact preview, migration workflow) + §6.1 (inheritance/override, seeding).

**Migrations:**
- `0023_catalog_instance_provenance` — `catalog_component_override` (spec §6.1's schema, including the
  partial unique index and the `status`/`orphaned_at`/`orphaned_reason` columns), the flat `field_name`
  `CHECK` covering §6.1's allowlist union, `fn_validate_catalog_component_override()` and its
  `BEFORE INSERT OR UPDATE` trigger — including the closed bypass (a new row must be created `active`; the
  active → orphaned transition requires the `app.catalog_migration_context` session flag; an already
  `orphaned` row accepts no further `UPDATE`) — nullable `network_interface.port_template_id`, nullable
  `integration_metric_mapping.metric_template_id`. No backfill of existing rows (all three new columns
  default `NULL`/absent for every pre-existing row, matching the specification's explicit "no backfill"
  instruction).

**Backend files:**
- `backend/app/api/v1/equipment.py`, `backend/app/api/v1/racks.py` — new override CRUD endpoints (spec
  §6.1's exact paths: `POST`/`PATCH`/`DELETE /{racks,equipment}/{id}/overrides[/{override_id}]`), gated by
  the asset's own `equipment:manage`/`rack:manage` permission — **not** `catalog:*`, and never
  `require_catalog_administrator` — per spec §6.1's explicit statement that overriding an installed field
  is ordinary asset editing, not catalog authoring. The `POST`/`PATCH` request schema for these endpoints
  has no `status`
  field at all: there is no way to request orphaning through them.
- `backend/app/api/v1/catalog_designer.py` — impact preview, migration-preview, migrate endpoints, gated by
  `require_catalog_administrator("catalog:migrate")`; the migration-preview response includes, per
  affected override, the required-disposition field from spec §6.1 (`carry_as_orphaned` / `discard`) for
  the admin to fill in before apply.
- `backend/app/application/catalog_migration_service.py` (new) — compatibility checking; per-asset atomic
  migration implementing spec §6.1's exact seven-step transaction (lock asset row → `SET LOCAL
  app.catalog_migration_context = 'on'` → repoint → reconcile overrides per confirmed disposition → one
  audit row → one outbox event → commit), with the bulk-tolerant per-asset-result-list semantics §5.9
  describes across the whole request. This service is the **only** code path in the entire codebase that
  ever issues the active → orphaned `UPDATE`.
- `backend/app/application/catalog_component_overrides.py` (new) — spec §6.1's allowlist table as Python
  constants, `validate(component_kind, field_name, value_type, value) -> None`, and the reset (`DELETE`)
  helper. Both the new `equipment.py`/`racks.py` endpoints (override create/reset) and
  `catalog_migration_service.py` (reconciliation during migration) import from here rather than
  duplicating the allowlist.
- `backend/app/domain/network/models.py`, `backend/app/domain/telemetry/models.py` — add the two nullable
  provenance FKs.
- `backend/app/db/models.py` — register `CatalogComponentOverride` (finding §1.2 item 1, corrected).

**Frontend files:** `frontend/src/features/catalog-designer/InstalledAssetsImpactPanel.tsx`,
`MigrationWizard.tsx` (including the per-override disposition picker for removed `stable_key`s);
an "Inherited"/"Overridden"/"Orphaned" badge (three states now, not two, per spec §6.1's `status` column)
added to the existing equipment detail view (`frontend/src/features/equipment/EquipmentDetailPage.tsx`)
wherever a seeded field is rendered, with a reset control that calls the `DELETE` endpoint above — visible
and usable by any role holding `equipment:manage`/`rack:manage`, not gated behind `useHasPermission("catalog:manage")`.

**Tests:** `backend/tests/api/test_catalog_migration.py` — per-asset-atomic bulk migration where one asset
has a stale `version` and the others still succeed (the concrete regression test §9.10 names); a removed
`stable_key` blocks migration until an explicit `carry_as_orphaned`/`discard` disposition is supplied, and
`carry_as_orphaned` preserves the value (never deletes it) while `discard` genuinely removes the row;
`reason` required and enforced; idempotent retry behavior per §5.9's correction-commit addition; a user
holding `equipment:manage` but not `catalog:migrate`/Administrator can create/reset an override but cannot
call the migration endpoints (proving the two authorization boundaries are genuinely independent, not one
masquerading as the other).
`backend/tests/integration/test_catalog_component_override_constraints.py` (new, matching spec §9.10's
expanded list) — the pin-check trigger rejects an override whose `catalog_model_revision_id` does not
match the asset's current `model_revision_id`; rejects a `stable_key` absent from that revision's templates
for the given `component_kind`; **a direct `INSERT` with `status='orphaned'` is rejected** (the closed
bypass — this is the test that would have caught the earlier, vulnerable version of this design); an
`UPDATE` attempting the active → orphaned transition **without** `app.catalog_migration_context` set is
rejected; the same transition **with** the flag set succeeds; any further `UPDATE` on an already-`orphaned`
row is rejected; the partial unique index allows an `orphaned` row and an `active` row for the same
`(managed_asset_id, component_kind, stable_key, field_name)` to coexist but rejects two simultaneous
`active` rows; a concurrency test proving an override write and a migration on the same asset serialize via
the shared `FOR UPDATE` lock rather than racing.

**Acceptance:** matches specification §18's second paragraph in full — install, clone+publish a newer
revision, accurate impact/compatibility preview, deliberate single-asset migration while a sibling asset
stays pinned, retiring an in-use revision leaves existing assets readable and blocks new selection unless
explicitly overridden. Additionally: no override value is ever silently lost during a migration — every
affected override is either carried forward, explicitly orphaned (value retained, visibly marked), or
explicitly discarded by an admin action, never defaulted; no code path outside the migration service can
ever produce an orphaned row, verified by the constraint test suite above, not merely by code review.

### 3.8 PR-8 — Monitoring-template seeding, regression, and documentation

**Scope:** the `MonitoringMetricTemplate` → `IntegrationMetricMapping` seed action (§6.1's last paragraph),
full regression pass, operator documentation.

**Backend files:** the seed action added to wherever `IntegrationMetricMapping` creation already lives
(`app/api/v1/telemetry.py`/its service — exact call site confirmed during PR-8 itself, not assumed here);
no changes to any driver/collector code (§6.2, §15 — explicitly out of scope).

**Tests:** end-to-end regression run of the **entire existing** `backend/tests/` and `frontend/src` suites
(confirming zero regression in rack/equipment/network/power/integration/3D workflows, per specification
§18's closing sentence); a full permission-matrix acceptance script exercising every `catalog:*` code
against every seeded role.

**Documentation:** an operator-facing README section (or a new `docs/` page, exact location decided at
PR-8 time) covering the draft→publish→migrate workflow, since this is the first admin-only feature area in
the product.

**Acceptance:** every acceptance criterion in specification §18 demonstrated in one place (a single
end-to-end script or documented manual walkthrough); no existing test regressed.

---

## 4. Rollout gate summary (explicit, as required)

1. PR-1 ships schema with zero behavioral change — safe to merge and deploy at any time, nothing reads it.
2. **PR-2 must ship, and its acceptance criterion (§3.2's closure test suite green) must hold, before PR-3
   is merged.** This is the literal ordering the task required: administrator-only enforcement and legacy
   catalog-authoring closure happen first; the new publishing workflow (first appearing in PR-3) is
   admin-gated from the moment it exists, with no window where it is reachable by a non-administrator and
   no window where the old side door is still open.
3. PR-4 through PR-8 may proceed in parallel once PR-3 is merged (PR-5/6/7 have no dependency on each
   other, only on PR-1/PR-3/PR-4 as tabulated in §3's table) — the gate in step 2 is the only hard ordering
   constraint; everything after it is a normal dependency graph, not a security gate.
4. No PR in this plan touches Phase 10B (network operations) or Phase 10C (spatial digital twin) surfaces.

---

## 5. Authorization boundary check: does "administrator-only" hold under custom permission grants?

Checked directly against `backend/app/domain/auth/models.py` and `backend/app/application/rbac.py` (the
same files verified in §1.1/§1.2), not assumed from the seeded default grants alone, per the explicit
instruction not to treat the seed as the entire authorization boundary.

**Correction to this plan's earlier conclusion.** An earlier version of this section examined the same
question and concluded no role-name check should be added, reasoning that this codebase's RBAC model is
exclusively permission-code-based everywhere else and that a hardcoded role check would be an inconsistent,
overly restrictive mechanism. That reasoning was wrong for this feature: the original Phase 10A product
instruction is explicit and specific — catalog creation, editing, publication, retirement, import, and
installed-asset migration are limited **to administrators**, not to "whichever custom role a deployment
happens to grant a `catalog:*` code to." Genericizing that into "permission-code-based, like everything
else" silently weakened an explicit product boundary. This plan now adds the narrowly-scoped check
correction 1 (this task) requires, and treats the prior conclusion as superseded, not merely refined.

**Confirmed against the actual RBAC implementation, not assumed:** `app/domain/auth/models.py`/
`app/application/rbac.py` have no first-class "Administrator" concept — no `is_superuser` flag, no
hardcoded role-name check anywhere today, `Role` a "normal table" per its own docstring, custom roles fully
supported, `AuthContext`/`get_auth_context()`/`require_permission()` all permission-code-only. That general
design is correct and untouched for every one of the ~40 other `require_permission()`-gated routes in this
codebase; it is not sufficient for catalog mutations specifically, because a principal holding `role:manage`
(itself `Administrator`-only by default) can create a custom role holding only `catalog:manage` — no
`Administrator` membership required — which would satisfy `require_permission("catalog:manage")` alone.

**Design, now specified in the specification's §9.1 (this plan implements it, does not re-derive it):**

- `AuthContext` gains `role_names: frozenset[str]`, populated by a query that joins outward from
  `RoleAssignment`/`Role` (`LEFT JOIN` to `RolePermission`/`Permission`, not an inner join starting from
  `Permission`) — so a role's membership is captured independent of whatever permissions that role
  currently grants; deriving role membership by joining *through* `RolePermission` would incorrectly drop a
  role that has been stripped of every permission, even though the user is still formally assigned to it.
- `require_catalog_administrator(code)` — a new dependency factory, the same shape as the existing
  `require_permission(code)`, checking **both** `ctx.has_permission(code)` **and**
  `ctx.has_role("Administrator")`, combined into one function (not two separate `Depends()` per route) so a
  future route cannot add the permission check while forgetting the role check, or the reverse.
- Applied to every route in §10's API table whose Authorization column reads `catalog:manage`/
  `catalog:publish`/`catalog:retire`/`catalog:import`/`catalog:migrate` — the seven mutation codes — and to
  the four legacy `catalog.py` endpoints PR-2 (§3.2) tightens. **Not** applied to `catalog:read`/
  `catalog:read_draft` (plain `require_permission()`, unchanged), keeping read access available to every
  role already granted those codes, per product principle 9 and this task's explicit instruction.
- `CatalogComponentOverride` create/reset (spec §6.1) is a distinct case: gated by the asset's own
  `equipment:manage`/`rack:manage` permission, no Administrator requirement — it is ordinary installed-asset
  editing, not catalog authoring. Only the migration service's own orphaning transition, reachable
  exclusively through the `catalog:migrate`-gated (and therefore Administrator-role-gated) migration
  endpoint, falls under this section's boundary. This distinction is spelled out in spec §6.1, not
  duplicated here beyond this one clarifying sentence.
- Matching `Role.name == "Administrator"` by string, not a new schema flag: `Role.name` is `UNIQUE`, so
  collision is not a risk; the accepted, disclosed caveat is that a future role-rename capability (none
  exists today — no `PATCH /roles/{id}` endpoint) could silently break this check if the seeded
  Administrator role were ever renamed. Recorded, not solved, since solving it would mean adding a new
  column to the foundational `role` table — out of proportion to a capability that does not exist yet.

**Regression tests** (PR-2, `backend/tests/api/test_catalog_rbac_closure.py`; full list in spec §9.10):

1. A synthetic custom role holding **only** `catalog:manage` (not named `"Administrator"`) is **rejected
   (403)** at every catalog-mutation endpoint, including the four tightened legacy endpoints — proving the
   permission code alone is now insufficient.
2. A user assigned `Administrator` with `catalog:manage`'s `role_permission` row explicitly deleted for the
   test is **rejected (403)** — proving a specific revocation still works even while role membership holds.
3. A user assigned `Administrator` and holding the permission **succeeds** — the positive case.
4. `catalog:read`/`catalog:read_draft` remain reachable by every role already holding those codes, with no
   Administrator requirement — the negative-space test proving reads were not accidentally tightened.

PR-2's acceptance criterion (§3.2) and every mutation-route description in §3's PR breakdown reference
`require_catalog_administrator()`, not the plain `require_permission()` this plan's earlier version used.

---

## 6. Decisions recorded and remaining status

| # | Decision | Resolution | Where recorded |
|---|---|---|---|
| 1 | Signing of `13165d3` | **Resolved.** Accepted as permanent, documentation-only unsigned history. Not amended, rebased, or force-pushed. New commits continue using the verified signing workflow. | §2, spec §0 |
| 2 | `CatalogComponentOverride` schema | **Resolved, and moved.** The schema, allowlist, and transaction design are now specification §6.1 — the single authoritative source. This plan references it (§1.3a) rather than duplicating it. | Spec §6.1; plan §1.3a, §3.7 |
| 3 | PR-1/PR-7 migration-scope inconsistency | **Resolved.** `app/db/models.py` registration is stated per-table, in the PR that actually creates each table's migration — PR-1 for the eight §4 tables, PR-6 for `CatalogImportJob`, PR-7 for `CatalogComponentOverride`. | §1.2 item 1, §3.6, §3.7 |
| 4 | "Administrator-only" under custom permission grants | **Corrected.** This plan's earlier conclusion (no role-name check) was wrong for this feature — the original product instruction specifically names administrators, not "whoever holds a `catalog:*` code." A narrowly-scoped `require_catalog_administrator()` dependency now requires both the permission and `Administrator` role membership on every catalog-mutation route, including the four tightened legacy endpoints; reads stay permission-only. | §5, spec §9.1, §10; plan §3.2, §3.3, §3.7 |
| 5 | `orphaned`-status bypass in the override trigger | **Found and closed during this update.** The version of the trigger recorded in decision 2's first pass let any write setting `status='orphaned'` skip the pin/component-existence check unconditionally — an ordinary caller could have inserted an arbitrary, never-validated "orphaned" row. Spec §6.1's corrected trigger requires every row to be created `active`, restricts the active → orphaned transition to a `SET LOCAL`-flagged migration transaction, and makes `orphaned` rows immutable thereafter. | Spec §6.1 |

**Now fully in the specification, not outstanding:** this update applies decision 2's schema, decision 4's
RBAC correction, and decision 5's trigger fix directly to
`docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md` (§0, §2, §6.1, §9.1, §9.10,
§10, §12) — the prior round's "exact proposed specification edit, not applied" is now applied, on this
new documentation-alignment branch, per this task's explicit instruction. The plan no longer carries
specification-shaped content for either decision; §1.3a and §5 state only what changed and why, pointing to
the spec sections that are now the single source of truth.

**Migration filename numbers** (`0017`–`0023`) remain illustrative, matching the specification's own
disclaimer — they will be re-derived from whatever the actual Alembic head is at the time each PR starts,
not reserved now.
