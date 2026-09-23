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

### 1.3a `CatalogComponentOverride` — approved design (resolves item 1 above)

**Approved for PR-7**, subject to the four requirements below, recorded here verbatim against each. This
content belongs in the specification's §6.1 as the authoritative design source — an exact proposed
specification edit is provided separately (not applied to the design branch by this plan; see the
accompanying report).

```text
CatalogComponentOverride  PK id, managed_asset_id FK→ManagedAsset CASCADE NOT NULL,
                         catalog_model_revision_id FK→CatalogModelRevision RESTRICT NOT NULL,
                         component_kind CHECK IN ('network_port','power_supply','monitoring_metric') NOT NULL,
                         stable_key VARCHAR(64) NOT NULL,
                         field_name VARCHAR(64) CHECK IN (<union of every allowlisted field_name below>) NOT NULL,
                         value_type CHECK IN ('text','numeric','boolean') NOT NULL,
                         value_text TEXT NULL, value_numeric NUMERIC(18,6) NULL, value_boolean BOOLEAN NULL,
                         CHECK (exactly one of value_text/value_numeric/value_boolean is non-null,
                                matching value_type — same NULL-pattern-CHECK style as CatalogGraphicMarker),
                         status CHECK IN ('active','orphaned') NOT NULL DEFAULT 'active',
                         orphaned_at TIMESTAMPTZ NULL, orphaned_reason VARCHAR(255) NULL,
                         CHECK ((status = 'active') = (orphaned_at IS NULL)),
                         created_by_user_id FK→User RESTRICT NOT NULL, created_at/updated_at
-- Partial unique index, not a plain UNIQUE constraint — same pattern already used by Alarm's
-- one-open-alarm-per-rule/subject index (app/domain/alarm/models.py):
--   UNIQUE INDEX ON (managed_asset_id, component_kind, stable_key, field_name) WHERE status = 'active'
-- At most one *active* override per field at a time; 'orphaned' rows from prior revisions persist as
-- history alongside it without colliding.
```

**Requirement — DB enforcement that the asset is pinned to the override's revision, and that `stable_key`
exists in that revision for its `component_kind`; lock behavior during concurrent migration.** A plain
`CHECK` cannot express either rule (both require reading other tables), so both are enforced by a
`BEFORE INSERT OR UPDATE` trigger, in the same hand-written-raw-SQL-trigger tradition as §5.4's
`fn_reject_write_on_non_draft_revision()`:

```sql
CREATE FUNCTION fn_validate_catalog_component_override() RETURNS trigger AS $$
DECLARE
  v_pinned_legacy_id UUID;
  v_bridge_rack UUID;
  v_bridge_equipment UUID;
  v_component_exists BOOLEAN;
BEGIN
  -- A row being set to (or already) 'orphaned' is explicitly exempt: 'orphaned' exists precisely to
  -- represent "this override's catalog_model_revision_id no longer matches what the asset is pinned to,
  -- and that is known and accepted" — see the migration-reconciliation requirement below.
  IF NEW.status = 'orphaned' THEN
    RETURN NEW;
  END IF;

  -- Lock the asset's Rack/Equipment row FIRST. This is the concurrency-safety requirement: the
  -- per-asset migration transaction (§5.9) takes the same FOR UPDATE lock on this row before it
  -- repoints model_revision_id, so an override write racing a migration on the same asset serializes
  -- on this row rather than reading a value the migration is mid-way through changing.
  SELECT model_revision_id INTO v_pinned_legacy_id FROM rack WHERE id = NEW.managed_asset_id FOR UPDATE;
  IF NOT FOUND THEN
    SELECT model_revision_id INTO v_pinned_legacy_id FROM equipment WHERE id = NEW.managed_asset_id FOR UPDATE;
  END IF;
  IF v_pinned_legacy_id IS NULL THEN
    RAISE EXCEPTION 'managed_asset % is not a Rack or Equipment instance', NEW.managed_asset_id;
  END IF;

  SELECT legacy_rack_model_revision_id, legacy_equipment_model_revision_id
    INTO v_bridge_rack, v_bridge_equipment
    FROM catalog_model_revision WHERE id = NEW.catalog_model_revision_id;
  IF v_pinned_legacy_id NOT IN (v_bridge_rack, v_bridge_equipment) THEN
    RAISE EXCEPTION 'override.catalog_model_revision_id % is not the revision managed_asset % is currently pinned to',
      NEW.catalog_model_revision_id, NEW.managed_asset_id;
  END IF;

  v_component_exists := CASE NEW.component_kind
    WHEN 'network_port' THEN EXISTS (SELECT 1 FROM network_port_template
      WHERE catalog_model_revision_id = NEW.catalog_model_revision_id AND stable_key = NEW.stable_key)
    WHEN 'power_supply' THEN EXISTS (SELECT 1 FROM power_supply_template
      WHERE catalog_model_revision_id = NEW.catalog_model_revision_id AND stable_key = NEW.stable_key)
    WHEN 'monitoring_metric' THEN EXISTS (SELECT 1 FROM monitoring_metric_template
      WHERE catalog_model_revision_id = NEW.catalog_model_revision_id AND stable_key = NEW.stable_key)
  END;
  IF NOT v_component_exists THEN
    RAISE EXCEPTION 'stable_key % does not exist for component_kind % on catalog_model_revision %',
      NEW.stable_key, NEW.component_kind, NEW.catalog_model_revision_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- BEFORE INSERT OR UPDATE ON catalog_component_override FOR EACH ROW
--   EXECUTE FUNCTION fn_validate_catalog_component_override();
```

Unlike §5.4's draft-only immutability trigger, this table is mutable throughout an installed asset's
operational life — this trigger governs *which* writes are valid, not *whether* writes are allowed at all.

**Requirement — explicit allowlist mapping each `(component_kind, field_name)` to its value type and
validation rule, narrow to what the installed-asset UI and migration workflow can actually support.** The
`field_name` `CHECK` above is a flat, DB-level backstop (the union of every allowlisted name, so no
arbitrary string is ever stored) — the *per-`component_kind`* pairing is application-layer only, same
reasoning the original proposal already gave (a `CHECK` cannot easily express a conditional set membership
per sibling column), now made concrete and narrow rather than left as an open-ended "allowlisted value":

| `component_kind` | `field_name` | `value_type` | Validation rule |
|---|---|---|---|
| `network_port` | `display_name` | `text` | 1–128 characters |
| `network_port` | `role` | `text` | one of `NetworkPortTemplate.role`'s own `CHECK` list (`uplink`/`access`/`management`/`stack`/`other`) |
| `network_port` | `speed_mbps` | `numeric` | integer-valued, `> 0` |
| `power_supply` | `label` | `text` | 1–128 characters |
| `power_supply` | `rated_current_a` | `numeric` | `> 0` |
| `monitoring_metric` | `default_collection_interval_seconds` | `numeric` | integer-valued, `> 0` |
| `monitoring_metric` | `default_warning_threshold` | `numeric` | finite |
| `monitoring_metric` | `default_critical_threshold` | `numeric` | finite |

Deliberately narrow: every field here is one the installed-asset UI (badge + reset control, §6.1) and the
migration-compatibility check (§5.9) already need to read/display/reconcile per the specification's own
text — no field is added speculatively. `backend/app/application/catalog_component_overrides.py` (new,
PR-7) is the single place this table lives as Python constants plus a `validate(component_kind, field_name,
value_type, value) -> None` function every write path calls before insert/update.

**Requirement — no row means inherited; an explicit row means overridden even if its value equals the
template default; reset deletes the row; handling of invalid/removed component keys during migration
without silently losing local values.** No row → inherited, computed by the API from the current template.
An `active` row → overridden, unconditionally (no value-equality comparison anywhere — matching the
original design's own stated reasoning). Reset (any status) → `DELETE FROM catalog_component_override
WHERE id = ...`, always allowed, no trigger involvement (delete is exempt from the pin-check by
construction — the function above only fires `BEFORE INSERT OR UPDATE`).

During migration (§5.9), for every `active` override belonging to the asset being migrated, the migration
service checks whether `(component_kind, stable_key)` still exists on the **target** revision:

- **Exists** → the service `UPDATE`s the override row's `catalog_model_revision_id` to the target revision
  — a normal write that passes the trigger cleanly, *provided* the asset's own `model_revision_id` is
  repointed **first**, within the same transaction (ordering requirement, stated explicitly in the
  per-asset transaction step list below).
- **Does not exist** (a removed `stable_key` — already required to surface as `compatible_with_warnings` in
  the migration preview per §5.11) → the preview response includes this override and **requires an
  explicit, admin-supplied disposition before apply**, generalizing the specification's own already-stated
  `NetworkInterface` disposition ("removed keys require an explicit keep-as-local or discard decision") to
  every `CatalogComponentOverride` row:
  - `carry_as_orphaned` → `UPDATE ... SET status = 'orphaned', orphaned_at = now(), orphaned_reason =
    'stable_key removed in target revision <id>'` — the **value is never deleted**; it becomes visible in
    the UI as local-only history no longer tied to the asset's current pinned revision.
  - `discard` → an explicit, admin-confirmed `DELETE` (the reset path, applied deliberately rather than as
    a migration side effect).
  - **No disposition supplied for an affected override blocks that asset's migration outright** — silence
    is refused, never defaulted either direction, which is the literal requirement ("without silently
    losing local values") applied to the ambiguous case, not just the unambiguous ones.

**Requirement — revision-pointer changes, override reconciliation, audit, and outbox writes commit or roll
back together per asset.** This extends, rather than replaces, §5.9's existing per-asset sub-transaction.
Final step order for each asset in a migration request:

1. `SELECT ... FOR UPDATE` the asset's `Rack`/`Equipment` row; check `if_match_version`.
2. `UPDATE rack/equipment SET model_revision_id = <target legacy id>, version = version + 1`.
3. For every `active` `CatalogComponentOverride` on this asset, apply its resolved disposition from step 1
   of §5.9 (carry-forward `UPDATE`, `carry_as_orphaned` `UPDATE`, or `discard` `DELETE`) — only reachable
   *after* step 2, so the pin-check trigger validates each carry-forward `UPDATE` against the asset's
   **already-updated** `model_revision_id`.
4. `write_audit_log(action="rack.migrate_revision"/"equipment.migrate_revision", ...)` — one row per asset,
   its `after` payload summarizing both the revision change and every override disposition applied, not a
   flood of one audit row per override.
5. `write_outbox_event(...)`.
6. Commit this asset's sub-transaction. A failure at any of steps 1–5 rolls back the entire sub-transaction
   — the revision repoint and its override reconciliation never commit separately, and neither commits
   without its audit/outbox pair, exactly as §5.9 already requires for the revision pointer alone, now
   explicitly spanning the override reconciliation too.

None of the above 1.3 items — 2, 3, and 4, item 1 now resolved above — are contradictions that block
planning; they are exactly the kind of "needs review before implementation" items the specification's own
thoroughness elsewhere makes conspicuous by their absence here. This plan proceeds using the item 2–4
proposals as **placeholders**, each marked in the PR breakdown as flagged for review, not silently treated
as approved. Item 1 (`CatalogComponentOverride`) is no longer a placeholder — it is an approved design per
the decision recorded above.

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
  itself does not re-derive from it).
- `backend/app/api/v1/catalog.py` — change all four mutating dependencies from
  `require_permission("rack:manage")`/`require_permission("equipment:manage")` to
  `require_permission("catalog:manage")`; the two list (`read`) endpoints keep their existing
  `rack:read`/`equipment:read` gate (read access stays broad, per product principle 9 — only *authoring*
  is tightened). Update this file's own module docstring to note the tightened requirement.

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
- `backend/tests/api/test_catalog_rbac_closure.py` (new): a user holding only `Engineer`'s permission
  set gets 403 from every mutating `catalog.py` endpoint (this is the concrete regression test the
  specification's §9.10 calls for); a user holding `Administrator` still succeeds; the two read endpoints
  remain reachable by every role that held `rack:read`/`equipment:read` before this PR (no read regression).
- `frontend/src/features/racks/RacksPage.test.tsx` / `EquipmentPage.test.tsx` (extend existing, or add):
  the inline-mint form no longer renders; the model/revision picker renders and drives `createRack`/
  `createEquipment` with a selected `model_revision_id` instead of newly-minted identity fields.

**Acceptance (this is the explicit rollout gate the task required):** at the end of this PR, no
authenticated user without a role holding `catalog:manage` can create a new `RackModel`/`RackModelRevision`/
`EquipmentModel`/`EquipmentModelRevision` row through any reachable path, frontend or API — verified by the
new `test_catalog_rbac_closure.py` suite. Stated as a permission-code claim, not a role-name claim
(`"without the Administrator role"` was this plan's own earlier, imprecise phrasing) — see §5's
authorization-boundary analysis for why: this codebase enforces every route by permission code alone, and
`catalog:manage` is held by `Administrator` at seed time but is not structurally tied to that role name.
This must be true and merged **before PR-3 opens the first new catalog-mutation endpoint.**

### 3.3 PR-3 — Catalog lifecycle backend

**Scope:** the full typed manufacturer/model/draft/validate/publish/retire/clone/compare backend, gated
entirely by the permissions PR-2 already seeded and enforced.

**Backend files:**
- `backend/app/api/v1/catalog_designer.py` (new router — §10's manufacturer, model, and revision-lifecycle
  endpoints only; graphics/markers go in PR-5, import/export in PR-6, migration in PR-7, all as additions
  to this same router file).
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
- Full 403 matrix from §9.10 for every new endpoint in this PR.

**Acceptance:** an administrator can create and publish a rack or equipment revision end-to-end via the
API, matching the first sentence of the specification's §18 acceptance criteria; no non-administrator can
reach any endpoint in this router (build on PR-2's closure, not a re-test of it).

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
- `backend/app/api/v1/catalog_designer.py` — add the graphics/marker endpoints from §10's table.
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
- `backend/app/api/v1/catalog_designer.py` — import/export endpoints.
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

**No longer blocked:** `CatalogComponentOverride`'s design is approved (§1.3a) — this PR implements it as
specified there, including its trigger, allowlist, orphan/reconciliation semantics, and the atomic
per-asset transaction step order.

**Scope:** §5.8/§5.9 (impact preview, migration workflow) + §6.1/§1.3a (inheritance/override, seeding).

**Migrations:**
- `0023_catalog_instance_provenance` — `catalog_component_override` (§1.3a's schema, including the
  partial unique index and the `status`/`orphaned_at`/`orphaned_reason` columns), the flat `field_name`
  `CHECK` covering §1.3a's allowlist union, `fn_validate_catalog_component_override()` and its
  `BEFORE INSERT OR UPDATE` trigger, nullable `network_interface.port_template_id`, nullable
  `integration_metric_mapping.metric_template_id`. No backfill of existing rows (all three new columns
  default `NULL`/absent for every pre-existing row, matching the specification's explicit "no backfill"
  instruction).

**Backend files:**
- `backend/app/api/v1/catalog_designer.py` — impact preview, migration-preview, migrate endpoints; the
  migration-preview response includes, per affected override, the required-disposition field from §1.3a
  (`carry_as_orphaned` / `discard`) for the admin to fill in before apply.
- `backend/app/application/catalog_migration_service.py` (new) — compatibility checking; per-asset atomic
  migration implementing §1.3a's exact six-step transaction (lock → repoint → reconcile overrides per
  confirmed disposition → one audit row → one outbox event → commit), with the bulk-tolerant
  per-asset-result-list semantics §5.9 describes across the whole request.
- `backend/app/application/catalog_component_overrides.py` (new) — the §1.3a allowlist table as Python
  constants, `validate(component_kind, field_name, value_type, value) -> None`, and the reset (`DELETE`)
  helper. Both `catalog_designer.py` (override CRUD on an installed asset) and
  `catalog_migration_service.py` (reconciliation during migration) import from here rather than
  duplicating the allowlist.
- `backend/app/domain/network/models.py`, `backend/app/domain/telemetry/models.py` — add the two nullable
  provenance FKs.
- `backend/app/db/models.py` — register `CatalogComponentOverride` (finding §1.2 item 1, corrected).

**Frontend files:** `frontend/src/features/catalog-designer/InstalledAssetsImpactPanel.tsx`,
`MigrationWizard.tsx` (including the per-override disposition picker for removed `stable_key`s);
an "Inherited"/"Overridden"/"Orphaned" badge (three states now, not two, per §1.3a's `status` column)
added to the existing equipment detail view (`frontend/src/features/equipment/EquipmentDetailPage.tsx`)
wherever a seeded field is rendered, with a reset control that calls the `DELETE` helper above.

**Tests:** `backend/tests/api/test_catalog_migration.py` — per-asset-atomic bulk migration where one asset
has a stale `version` and the others still succeed (the concrete regression test §9.10 names); a removed
`stable_key` blocks migration until an explicit `carry_as_orphaned`/`discard` disposition is supplied, and
`carry_as_orphaned` preserves the value (never deletes it) while `discard` genuinely removes the row;
`reason` required and enforced; idempotent retry behavior per §5.9's correction-commit addition.
`backend/tests/integration/test_catalog_component_override_constraints.py` (new) — the pin-check trigger
rejects an override whose `catalog_model_revision_id` does not match the asset's current
`model_revision_id`; rejects a `stable_key` absent from that revision's templates for the given
`component_kind`; an `orphaned`-status write bypasses the pin check; the partial unique index allows an
`orphaned` row and an `active` row for the same `(managed_asset_id, component_kind, stable_key,
field_name)` to coexist but rejects two simultaneous `active` rows; a concurrency test proving an override
write and a migration on the same asset serialize via the shared `FOR UPDATE` lock rather than racing.

**Acceptance:** matches specification §18's second paragraph in full — install, clone+publish a newer
revision, accurate impact/compatibility preview, deliberate single-asset migration while a sibling asset
stays pinned, retiring an in-use revision leaves existing assets readable and blocks new selection unless
explicitly overridden. Additionally: no override value is ever silently lost during a migration — every
affected override is either carried forward, explicitly orphaned (value retained, visibly marked), or
explicitly discarded by an admin action, never defaulted.

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

**This codebase has no first-class "Administrator" role concept anywhere in enforcement code.** No
`is_superuser` flag on `User`, no hardcoded role-name check, no special-casing in `get_auth_context()`/
`require_permission()`. Every protected route — every existing one, and every one this plan proposes — is
gated by an exact permission-code match against the `permission`/`role_permission`/`role`/
`role_assignment` join. `Role` is, per `rbac.py`'s own module docstring, "a normal table," and custom roles
are explicitly, fully supported. This is true of every sensitive permission in the system today
(`user:manage`, `role:manage`, `organization:manage`, `audit:view`) — not something Phase 10A introduces or
could opt out of by adding a `catalog:*` family that behaves differently from every other permission
family already in the codebase.

**Concrete answer:** `catalog:*` permissions alone genuinely could authorize a principal that does not
hold the literal `Administrator` role — but only through one specific, pre-existing path: a principal
holding `role:manage` (itself `Administrator`-only by default seed) can create a new custom role and grant
it any `permission` row, including any `catalog:*` code, through the existing, general-purpose role/
permission management surface. None of that is new, and none of it is specific to this feature —
`role_permission` rows can only ever be inserted by code gated on `role:manage`, or by a trusted migration
script run outside the request path. So the true transitive boundary for "who can ultimately author
catalog definitions" is **"whoever holds `role:manage`, plus whoever such a holder chooses to delegate
to"** — exactly the boundary that already governs `user:manage`, `audit:view`, and every other
Administrator-only-by-default permission, unaffected by Phase 10A's own RBAC seed migration (§3.2).

**Decision: this plan does not add a hardcoded role-name check** (e.g. `require_role("Administrator")`
alongside or instead of `require_permission("catalog:manage")`), for two concrete reasons:

1. **No such pattern exists anywhere else in this codebase.** Adding one here, and only here, would be a
   second, inconsistent enforcement mechanism sitting next to the permission-code system every other route
   trusts exclusively — a real reasoning hazard for anyone auditing which routes are governed by which
   rule.
2. **It would remove a legitimate, currently-available least-privilege capability, not close a gap.** An
   organization's `Administrator` may deliberately delegate catalog authoring to a narrower custom role
   (e.g. "Catalog Editor," holding only the seven `catalog:*` codes and nothing else — not `user:manage`,
   not `role:manage`, not `organization:manage`) without granting full `Administrator` privileges. A
   hardcoded role-name check would make that impossible, forcing every catalog author to hold full
   `Administrator` — a *worse* security posture than the one already available, and inconsistent with how
   every other permission in this system supports exactly this kind of scoped delegation today.

**What this plan does instead**, since assuming the seeded grants are the entire boundary was explicitly
ruled out:

- States the boundary precisely, as above, rather than leaving "administrator-only" resting on an unstated
  assumption about the seed migration's specific defaults.
- Corrects PR-2's acceptance criterion (§3.2) from an earlier, imprecise role-name framing to the accurate,
  tested claim — permission-code possession, not role identity (done above).
- Adds two tests to PR-2's suite (`backend/tests/api/test_catalog_rbac_closure.py`), making the boundary
  explicit and regression-proof rather than implicit in the seed data:
  1. A synthetic role holding **only** `catalog:manage` (deliberately not named `"Administrator"`, holding
     no other permission) **succeeds** at a catalog-mutation endpoint — proving enforcement is genuinely
     permission-code-based, not accidentally dependent on a role-name string anywhere in the
     implementation.
  2. A user assigned the real `Administrator` role, with that role's `catalog:manage` grant explicitly
     revoked for the test (its `role_permission` row deleted), **fails** (403) at the same endpoint —
     proving no special case exists for the `Administrator` role name itself, and that revoking a specific
     grant genuinely revokes the specific capability, independent of role identity.
- Notes, for operational awareness rather than as a code change: what actually keeps "administrator-only"
  true for catalog authoring in practice is `role:manage` and `organization:manage` staying tightly held —
  exactly as it already is for every other Administrator-only capability in this product. This plan does
  not change, weaken, or need to change that existing boundary.

---

## 6. Decisions recorded and remaining status

| # | Decision | Resolution | Where recorded |
|---|---|---|---|
| 1 | Signing of `13165d3` | **Resolved.** Accepted as permanent, documentation-only unsigned history. Not amended, rebased, or force-pushed. New commits continue using the verified signing workflow. | §2 |
| 2 | `CatalogComponentOverride` schema | **Resolved.** Approved for PR-7, with the pin/component-existence trigger, the narrow field allowlist, the `active`/`orphaned` reconciliation semantics, and the six-step atomic per-asset transaction all specified. | §1.3a, §3.7 |
| 3 | PR-1/PR-7 migration-scope inconsistency | **Resolved.** `app/db/models.py` registration is now stated per-table, in the PR that actually creates each table's migration — PR-1 for the eight §4 tables, PR-6 for `CatalogImportJob`, PR-7 for `CatalogComponentOverride`. | §1.2 item 1, §3.6, §3.7 |
| 4 | "Administrator-only" under custom permission grants | **Resolved.** No hardcoded role-name check added (would be inconsistent with this codebase's exclusively permission-code-based RBAC and would block legitimate least-privilege delegation); the true boundary (`role:manage`) is stated explicitly, PR-2's acceptance criterion is restated in permission-code terms, and two new regression tests make the boundary explicit rather than implicit. | §5, §3.2 |

**Outstanding, not part of this plan's own scope:** decision 2's design (§1.3a) is now approved for
implementation in this plan, but it is specification-shaped content that belongs in the design
specification itself (`docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md`
§6.1), which this plan does not edit — per the task's own instruction, the exact proposed specification
edit is reported separately, alongside this update, for your decision on whether to apply it to the design
branch. The same applies, more lightly, to decision 4's finding, which is a clarification the
specification's own RBAC section (§9.1) could usefully state explicitly.

**Migration filename numbers** (`0017`–`0023`) remain illustrative, matching the specification's own
disclaimer — they will be re-derived from whatever the actual Alembic head is at the time each PR starts,
not reserved now.
