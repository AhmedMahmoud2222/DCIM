# User & Group Management

Administrator-facing management of users, groups, permissions and site/rack access.
Everything below is enforced on the server; the UI only mirrors it.

## Concepts

| Concept | Meaning |
| --- | --- |
| **Role** (existing) | `RoleAssignment` to a named role (Administrator, DCIM Manager, Engineer, Operator, Viewer). Unchanged by this feature. |
| **Group** (new) | Named set of users carrying permission grants (`allow` / `deny`) and site/rack access. |
| **Site access** | A group grants a site with `rack_scope = all` (every rack currently placed there) or `selected` (only listed racks). |

## Effective permissions (SEC-ARCH-02, #163)

The owner approved **Option A: permission-local entitlements** on 2026-10-15. Each request uses the database's current grants. Permissions, membership, site/rack grants and organization reach are loaded in **one PostgreSQL statement**, so concurrent changes cannot combine an old permission with a new resource grant from a different committed state.

1. Bind every group allow to **that group's own** site/rack grants. A permission-only group grants no site-bound data reach.
2. Union resource scopes only between sources granting the **same permission**. `rack:manage` at A plus `rack:read` at B never grants manage at B. Within that permission/site, `all` overrides `selected`; otherwise selected racks union.
3. A global role grants **only its own permissions** globally. Global Viewer plus group manage at A can read globally and manage only at A. It cannot activate a scoped group's unrelated unfiltered write permission.
4. User-wide explicit deny beats every allow, including global roles. Permission codes match exactly; `manage` does not imply `read`. Groups do not nest.
5. Preserve SEC-ARCH-01 scoped-role eligibility and role-name restrictions described below. This change does not activate role combinations that #134 intentionally rejects.

### Resource boundaries

- A restricted permission sees a selected rack only while its current placement is in the site under which it was granted. An unrelated read permission does not make a stale write grant follow a moved rack.
- Equipment visibility follows its rack, or its room for rack-less equipment in an all-racks site. Unplaced assets remain invisible to restricted entitlements.
- Locations are filtered by the scope of the specific location/organization read permission. Writes validate their own permission on all affected resources, including move source and destination.
- Out-of-scope IDs return 404; a missing/inactive permission returns 403. Legitimate in-scope optimistic concurrency conflicts remain 409. Denial commits no successful mutation, audit or outbox event.
- Scope-aware codes are pinned by `SCOPE_AWARE_PERMISSIONS` in `access_control.py`. Other data permissions remain inactive unless independently granted by a global role. The global status of another permission never changes this.
- Filtered thermal views retain their existing bounded read exception: required cooling/spatial/telemetry scopes intersect before data is queried. Optional alarm/power details are included only when that permission covers the evaluated room's population; partial coverage omits the optional detail rather than widening access. These grants still cannot unlock the corresponding unfiltered endpoint.
- Secondary gates (spatial overlays and floor-plan rack placement) validate the secondary permission's own scope. A global spatial/floor-plan permission cannot lend global reach to a scoped cable read or rack placement permission.
- User/group administration uses the specific `user:read`, `user:manage`, `group:read` or `group:manage` scope. Directory masking, conferral and resulting-authority checks cannot substitute a global Viewer's data visibility for administrative authority.

The legacy effective-access `unrestricted` and `sites` fields describe **aggregate reach**, not the scope of every permission. `permission_scopes` is authoritative per permission; the admin panel shows those scopes beside each permission. Inactive grants have scopes too, for bounded thermal reads and preflight reporting. Selected rack IDs in the API are filtered to current visible placement and the querying administrator's scope; the privileged preflight export retains latent grants.

## Security safeguards

* **Privilege escalation**
  * An actor can `allow` only permissions they hold; deny grants are always allowed.
  * Site grants are compared by *containment*: the actor's own scope must contain everything handed out. A site needs the actor's access to it, `all` racks needs the actor's `all`, and selected racks must already be visible to the actor on that site. A delegate limited to selected racks therefore cannot grant `all`.
  * Adding a user to a group requires holding every allowed permission over that group's whole site/rack scope. Permission replacement and site replacement also validate this combination for empty groups, before membership is added.
  * A global legacy role can be assigned only if the actor independently holds all its permissions globally and has unrestricted administrative authority.
  * Users cannot edit or delete a group they belong to, or change their own memberships.
  * **Strict outranking.** Effective authority maps each permission to its own site/rack scope. Every target entitlement must be contained in the actor's same-permission entitlement; raw/latent grant reach must also be contained. Authority remains a partial order without numeric rank. An actor may administer a user only if the user's permissions and scope both lie within the actor's (`target <= actor`) and the actor's do not lie within the user's (`not actor <= target`). Visible equal authority (peers), wider permission sets, and incomparable permission sets are refused with 403; out-of-scope principals are masked with 404 before authority comparisons. This covers user update/deactivate/re-password/regroup/delete and every group change that reaches a member (permissions, site access, rename, delete, membership add or remove): the actor must strictly outrank every user the change affects. Consequence: two principals with identical authority, including two global Administrators, cannot administer each other through the API; a strictly more senior principal must do it.
  * Creating a user or granting a group is still delegation: the actor may confer only authority they hold (the grant checks above), which may leave the new principal equal to the actor. The actor cannot administer that principal afterwards.
  * **Resulting authority.** The strict check above runs before the change. Removing a deny (a deny membership, a deny grant, or a whole deny group) can restore allow grants the target already holds elsewhere, so after the change is flushed and before anything commits, every surviving principal it can affect must still satisfy `target <= actor` (equality is allowed, exceeding or becoming incomparable is not). This covers user group replacement, group member add/remove, group permission replacement, group site-access replacement, group deletion and user creation. On failure the whole transaction is rolled back: membership and grant rows, password and refresh-token changes, and the audit row.
  * **No self-membership changes.** Nobody may add themselves to or remove themselves from a group, on either the user route or the group route, because a group can carry deny grants. Changing your own name or password is still allowed.
  * **Concurrency and lock order.** Every authority-changing route (user create/update/delete, group update/delete/members/permissions/site-access) takes the exclusive transaction-scoped advisory lock `dcim.authority_change` first, then re-reads the actor's effective access and re-checks their `user:manage`/`group:manage` permission and active flag. Scope is derived from current rack placement, so the only two writers of rack placement (`placement_service.move_rack` and `retire_rack_placement`, used by the rack create/move/retire routes and the bulk-import rack commit) take the same lock **shared**. A relocation therefore waits for any open authority decision to commit, and an authority decision waits for any open relocation and then reads its result; relocations do not block each other. Global order: `dcim.authority_change` (exclusive or shared) -> `dcim.admin_invariant` -> row locks (import job, rack, placement, group, user). The power-topology and catalog/alarm/discovery locks are never taken in a transaction that also takes the authority lock. A new writer of rack placement, or of room/floor/building parentage, must take the shared lock first (`tests/integration/test_pr67_authority_relocation_race.py` guards the first case). Equipment placement does not enter the authority comparison.
* **Scoped administration.** A site-restricted administrator sees and manages only users and groups whose grants lie inside their own scope. Users and groups outside it are 404 in lists, reads, effective-access and every write, so site names, user emails and group grants of other sites are not disclosed. Missing and out-of-scope principal IDs share the same generic 404 detail, including group assignment and membership batches. Visibility of the whole requested batch is checked before permission comparisons. Users with no groups (and no roles) are visible, so they can be onboarded.
* **Last administrator.** The surviving administrator must be an active, **unrestricted** user (holding a global role) whose effective `user:manage` and `group:manage` permissions are each genuinely global after explicit denies. Site-restricted and selected-rack administrators, inactive users, users denied either permission and users holding only one of them do not count. Any change that would leave none (deny grants, membership, group deletion, deactivation, deletion) fails with 409 and the whole transaction is rolled back (user, membership, token, role-assignment, group-grant, audit and outbox rows). A transaction-scoped advisory lock (`dcim.admin_invariant`, taken after `dcim.authority_change`) serialises concurrent changes, so two requests cannot each remove a different administrator. Users cannot deactivate or delete themselves. With strict outranking and the self-membership rule, an actor who may change an unrestricted administrator must outrank them, so this check is defence in depth: it is reachable only from a legacy database state that already has no qualifying unrestricted administrator.
* **Validation.** Names are trimmed, length-limited and reject control characters; group names are unique case-insensitively; permission codes must exist; passwords need 12 to 256 characters; list sizes are capped; racks granted under a site must currently be placed in it.
* **Sessions.** Deactivating a user revokes their refresh tokens and their access token stops working on the next request.
* **Audit.** Every mutation writes an audit entry in the same transaction (`user.create|update|activate|deactivate|delete`, `group.create|update|delete`, `group.members.update`, `group.permissions.update`, `group.site_access.update`) with before/after values. Passwords are never logged.

## API

| Method and path | Permission |
| --- | --- |
| `GET /users`, `GET /users/{id}`, `GET /users/{id}/effective-access` | `user:read` |
| `POST /users`, `PATCH /users/{id}`, `DELETE /users/{id}` | `user:manage` |
| `GET /groups`, `GET /groups/{id}`, `GET /groups/permission-catalog` | `group:read` |
| `POST /groups`, `PATCH|DELETE /groups/{id}`, `PUT /groups/{id}/members`, `PUT /groups/{id}/permissions`, `PUT /groups/{id}/site-access` | `group:manage` |
| `GET /racks?site_id=` (new filter) | `rack:read` |

`user:read`, `group:read` and `group:manage` are new and seeded to the Administrator role only (migration `0031_user_groups`).

## UI

* **Admin > Users** (`/admin/users`): search, create, edit, activate or deactivate, delete, assign groups, reset password, and an effective-access panel showing the granting role or group for each permission, denied and inactive permissions, and visible sites and racks.
* **Admin > Groups** (`/admin/groups`): create, edit, delete; tabs for members, per-permission allow/deny, and site and rack access.

## Migration impact

`0031_user_groups` only adds tables (`user_group`, `user_group_member`, `user_group_permission`, `user_group_site_access`, `user_group_rack_access`) and three permission rows. No existing user, role, role assignment or grant changes. Downgrade removes the tables and the three permissions.

One behavioural note: a `role_assignment` with a non-global `scope_type` previously granted its permissions globally (Phase 1 decision H7). Such rows cannot be created through the API today. If any exist, the holder is now treated as site-restricted, with the same fail-closed permission filtering. Since SEC-ARCH-01 the valid `scope_type` values are `global`, `site` and `organization`; `building` was removed by migration `0054_role_assignment_scope_types` (see Migration / upgrade note below).

## Scope semantics (SEC-ARCH-01, #57)

Legacy role eligibility uses aggregate group/organization reach (see Organization scope), while resource enforcement now uses the specific permission's source-bound scope. SEC-ARCH-02 deliberately preserves SEC-ARCH-01's conservative admission rule: a scoped assignment is honoured only when the holder's whole reachable site scope lies inside the assignment's scope, and otherwise contributes neither permissions nor its role name (so it cannot satisfy `Administrator` membership checks).

| Combination | Behaviour | Status |
|---|---|---|
| Global role | Its own permissions apply globally; unrelated group permissions keep their own scopes. | SEC-ARCH-02. |
| Global role plus any scoped assignment | Scoped assignment still contributes nothing; unrelated group grants remain permission-scoped. | SEC-ARCH-01 eligibility preserved. |
| Site-scoped assignment, group scope inside that site (all racks, or selected racks) | Role permissions apply to exactly the granted sites and racks. | Implemented. |
| Site-scoped assignment, group scope includes any other site | The assignment contributes nothing (denied, not narrowed). A user who needs the role on one site of several must hold it through a group allow-grant. | Implemented. Narrowing to the assignment's site is an owner decision (below). |
| Site-scoped assignment, no group grants | Permissions held, no data visible. | Unchanged. |
| Organization-scoped assignment, group sites all inside that organization (or no group sites) | Role permissions apply to every site under the organization. The holder stays site-restricted. See Organization scope. | Implemented. |
| Organization-scoped assignment, any group site outside that organization | Contributes nothing (denied, not narrowed). | Implemented. |
| Organization-scoped assignments naming two or more distinct organizations | All of them contribute nothing. | Implemented. |
| Organization-scoped plus site-scoped assignment | The organization widens reachable sites, so the site assignment is honoured only if that site is the only reachable site. | Implemented. |
| Building-scoped assignment | Not a valid scope. Migration `0054_role_assignment_scope_types` removes it. Any legacy row blocks the upgrade. | Removed (SEC-ARCH-01, #57). |
| Rack-limited group grant (`selected`) | Rack-level scope applies inside the site; a site-scoped assignment for that site is honoured and still limited to the selected racks. | Implemented. |
| Tenant | There is no tenant entity. The closest boundary is the organization, derived from the sites a user is reached through (group grants or an organization assignment): organizations without a reachable site are invisible (404). No tenant-level role assignment or grant exists. | Not implemented. |
| Group allow-grants | Unaffected by role scope. | Unchanged. |

Owner decisions still open: narrowing a site-scoped assignment to its own site (assignment-local intersection) versus the current containment rule or a user-wide union; the tenant model; scoping of the modules a restricted user cannot reach (alarms, telemetry, power, dashboard, floor plans, spatial, impact, discovery, integrations, collectors, managed assets, audit), which stay blocked.

## Organization scope (SEC-ARCH-01, #57)

An `organization` RoleAssignment grants its role's permissions across every site under one organization, in addition to group grants. Implemented in `load_effective_access` (`app/application/access_control.py`), with `organization_site_ids`, `scope_with_full_sites` and `scoped_role_is_contained`. The chain is Organization -> Country -> City -> Site.

* **Reach.** Every site under the organization is added to the user's reach as `rack_scope = all`. An `all` grant supersedes a `selected` grant only for the same permission. Organization reach does not widen unrelated group permissions.
* **Conditions.** The assignment is honoured only when all of the following hold. Otherwise it contributes nothing:
  * the user has no `global` role (a global user is already unrestricted, so organization rows are ignored);
  * every `organization` assignment of the user names the same single organization; two or more distinct organizations fail closed;
  * every site the user reaches from group grants lies inside that organization. A user with no group sites satisfies this, so the assignment alone grants visibility of all sites under the organization.
* **Permissions.** The admitted organization role grants its own permissions across its organization. Group allows stay bound to their own group sites/racks; they cannot borrow the organization role's reach. Explicit deny grants still win. Permissions that are not site-aware remain inactive for the restricted holder (see Fail closed for unscoped endpoints above).
* **Never global.** The holder stays site-restricted. Only a `global` assignment makes a user unrestricted.
* **No role name.** Permission entries are added, but the role name is not, so role-name checks such as `Administrator`, catalog administration and bulk import are not satisfied by an organization assignment.
* **Interaction with site assignments.** Reach is computed before site containment, so an organization assignment can make a co-existing site-scoped assignment fail its containment check. It is then honoured only if that site is the only reachable site.
* **Re-parenting invariant (forward-looking).** Organization reach is a live lookup through Organization -> Country -> City -> Site, recomputed on every request, not cached at assignment time. No route today re-parents a site, city or country (`locations.py` has only create routes for those levels; `PATCH /rooms` cannot change a room's parent), so this cannot yet be exploited. Any future route that re-parents a site, city or country must take the `dcim.authority_change` shared lock first (the same rule already stated above for rack placement and room/floor/building parentage) and should ship with a dedicated regression test, the same way `test_organization_scoped_role_reaches_a_site_created_after_the_assignment` pins the (intended) dynamic-expansion behavior for newly created sites.

## Migration / upgrade note (SEC-ARCH-01, #57)

Migration `0054_role_assignment_scope_types` removes `building` from the allowed `scope_type` values and adds `organization`. Before upgrading past 0054, an owner must check for legacy rows:

```sql
SELECT scope_type, count(*) FROM role_assignment GROUP BY 1;
```

If any `building` rows exist, the upgrade refuses and raises an error. The message in `backend/migrations/versions/0054_role_assignment_scope_types.py` is the authoritative remediation: review each row, then delete it or re-create it with an explicit `site`, `organization` or `global` scope, and run the upgrade again.

* Legacy `building` rows are never converted automatically, because any other scope could widen access. They grant nothing today, so deleting one does not reduce effective access. Re-creating one with a wider scope does widen it.
* Downgrade refuses while `organization` rows exist. Remove or re-scope them first.

## Known limitations

* Endpoints outside locations, racks and equipment are not site-scoped yet (see fail-closed rule above).
* The UI creates group-only (site-restricted) users. Legacy role assignment is available on `POST /users` (`role_name`) but has no UI, and roles cannot be changed after creation.
* Deleting a user who owns imports or catalog revisions is refused (409); deactivate instead. Users holding a legacy role delete normally.
* Denied escalation attempts return 403/404 but are not written to the audit log.
* Effective access is computed per request with a handful of queries; very large tenants may want caching.
* SEC-ARCH-01 (#57): `building` scope is removed and `organization` scope is added, with the migration path for legacy `building` rows (see Migration / upgrade note). SEC-ARCH-02 implements source-bound group permission scopes. Relaxing SEC-ARCH-01's conservative scoped-role admission remains separate work; this change does not activate suppressed site/organization role combinations.
* A user in two groups whose grants span different sites combines access across both groups. This is pre-existing, documented behaviour (see Effective permissions and Site and rack scope). SEC-ARCH-01 does not change it.

## Import jobs and site-restricted users (owner-approved uploader-access rule)
Operations (bulk-import pipeline): upload and template download per domain (`POST|GET /racks|equipment|catalog/import-*`), and on `/import-jobs/{id}`: status, `/rows` (preview, `?status=invalid` for validation errors), `/report`, `/commit`, `/cancel`. There is no list or retry endpoint.

Current behaviour: a site-restricted caller may touch a job only if they uploaded it; any other job answers a 404 identical to a missing id, for every operation. Unrestricted callers keep the earlier behaviour (a holder of `rack:read` / `equipment:read` can read any job of that type; `catalog` jobs need `catalog:read`).

What the acceptance tests show (`tests/api/test_pr68_import_job_acceptance.py`):
* `rack:import`, `equipment:import` and `catalog:import` are not site-aware, so they are inactive for a restricted user. A restricted user therefore cannot upload, commit or cancel, and the only jobs they can "own" are ones uploaded while they were unrestricted. Today that needs a direct database change (roles cannot change through the API).
* Uploader ownership alone does not equal site scope: a historical owner keeps read access to a job whose rows describe rooms outside their current scope. On 2026-10-01 the owner explicitly selected **keep uploader access**, accepting this read-access contract. The tests pin that approved behavior; import write permissions remain inactive for restricted users.
* No site or rack link has been added to jobs and the scope model is unchanged.

Owner decision (2026-10-01): preserve uploader-only read access for historical uploads, including rows and reports outside the uploader's current site scope. Other restricted callers still receive the same 404 as a missing job. This approves the existing read contract only; it does not enable restricted-user upload, commit or cancel, create a tenant model, or add site/rack linkage to jobs. `*:import` stays outside `SCOPE_AWARE_PERMISSIONS`. Any later site-aware import-write feature requires separate scoped design and authorization.

## Break-glass and recovery (current state, not implemented here)
`backend/scripts/create_admin.py` is the only out-of-band path. It needs shell access and the application database credentials, creates a *new* global Administrator and refuses (or, with `--if-exists skip`, silently leaves) an existing email. It cannot reset a password, deactivate or demote an existing account, or revoke refresh tokens. It writes no audit entry and needs no approval. Because global Administrators are peers, a compromised or departed Administrator cannot be deactivated through the API by another Administrator, so recovery today means direct database edits. A controlled peer-recovery procedure (separate approvers, an audit record, credential and session revocation) is a separate operational-security follow-up and deliberately not part of this change; no Super Administrator role or numeric rank is introduced.

## Tenant model: implemented behaviour and what stays outside it (issue #57 stays open)
Implemented: data scope is the pair (sites, racks) held through groups, plus the sites of an organization assignment (see Organization scope). Site-aware modules (organization/location read, racks, equipment, rack placement) filter by that scope and answer 404 for out-of-scope ids. Administering another principal (user and group routes) is bounded by strict outranking on permissions and scope, as described above, evaluated under the authority lock.

Outside the model today: there is no tenant entity that owns users, groups, import jobs or catalog data. User and group directories are readable by any holder of `user:read` / `group:read`; a site-restricted actor sees only the users and groups whose grants lie inside their own scope (see Scoped administration), and unrestricted holders see all of them. Catalog data is global manufacturer data. Alarms, telemetry, power, dashboard, floor plans, discovery and integrations are not site-scoped and stay disabled for restricted users. Import jobs carry no site link (see the PR #68 contract note). Two principals cannot be separated into tenants beyond what site/rack grants express; a real tenant boundary needs an owning-tenant column on users, groups and jobs and is a separate design.


## SEC-ARCH-02 rollout and compatibility

No schema or data migration is required: existing grant rows already identify their source. Existing global-role permissions and same-source legitimate operations are preserved. Cross-site combinations, permission-only groups paired with scope-only groups, all-racks reads paired with selected writes, global Viewer plus scoped write/admin grants, and unrelated organization-reach borrowing intentionally narrow.

Before a separately authorized rollout, run the **new code's** read-only preflight against a restored representative database or an authorized read-only connection:

```powershell
# From backend, with the existing environment configuration.
python scripts/sec_arch_02_preflight.py --output sec-arch-02-preflight.json
```

The CLI opens a READ ONLY, REPEATABLE READ transaction. It reports impacted user IDs, permission sources, before/after active status, raw site/rack scopes (including latent selected-rack grants), proposed narrow entitlements, and surviving genuinely global administrators. It never applies grants. Protect the report like an access-control export (see *Handling the preflight report* below). The owner must review intended reach and approve any additional explicit source-local grant; do not automatically recreate the old permission/site cross-product. Abort rollout if no qualifying global administrator remains.

### Handling the preflight report

The report lists user IDs, permission codes, grant-source names/IDs and raw site/rack scopes. Treat it as confidential access-control data.

- Use `--output`. The file is created owner-only (`0600`) and the command fails if the path already exists or is a symlink; it never overwrites. Without `--output` the report goes to stdout with a warning on stderr, so avoid it in shared terminals, CI logs and pipelines that are captured.
- On Windows the POSIX mode is not enforced: write into a directory only the operator can read, or restrict the ACL (`icacls <file> /inheritance:r /grant:r "%USERNAME%:F"`).
- Never upload the report as a CI artifact, attach it to an issue or PR, or commit it (`sec-arch-02-preflight*.json` is git-ignored as a safety net). Share it only over an access-controlled channel with the owner.
- Delete it after the rollout decision is recorded; keep only a sanitized summary (counts, no IDs).
- On failure the command prints only the exception class name, so errors cannot leak connection details or report content. Run it with debug tooling only on an authorized host.

Validate both fresh and populated PostgreSQL states, then require independent exact-HEAD Jules acceptance and all required CI before merge. Deployment and production grant changes require separate owner authorization. Reverting to the flattened evaluator reintroduces F1/F2; use a reviewed forward fix or suspend affected operations instead of silently restoring that behavior.

Some descriptions above retain SEC-ARCH-01's historical terminology for role eligibility. For **group composition and resource enforcement**, this section and the permission-local rules at the top supersede the previous user-wide union/global-override contract. Site-role admission, organization-role admission, building removal, deny-all router restrictions, and role-name checks remain unchanged.

## Route authorization guard and directory masking (issue #195, P2 wave 1)
* **Route guard.** `backend/tests/api/test_route_authorization_inventory.py` derives, from the FastAPI app, which control guards each of the API operations (permission code, permission plus Administrator role, handler-authorized, collector HMAC, public) and requires the result to equal the reviewed `backend/tests/api/route_authz_baseline.py`. A new, removed or re-guarded route fails the backend CI job until that file is edited in the same pull request, so the change is a reviewable diff. Unauthenticated (`PUBLIC`) and handler-authorized (`MANUAL`) routes need an allowlist entry with a reason; `MANUAL` entries must name an existing test. State-changing routes may not be guarded only by a read-type permission (three documented dry-run exceptions), permission codes must be seeded, and nothing may be served that the guard cannot classify (hidden OpenAPI entries, mounts, websockets). `test_route_authorization_guard_mutations.py` proves the guard fails on eight audit mutations. The guard reads FastAPI's private include-routing objects (observed in 0.156.x); that dependency is isolated in `route_authz_guard.py` and a contract check fails loudly if FastAPI changes it.
* **Cable neighbors.** `POST /cables/from-neighbor/{id}` checks that both resolved ports are visible to the caller before any state check. A neighbor with a port outside the caller's scope answers exactly like a missing neighbor (404 naming only the supplied id); its state and its ports are never observable.
* **Group membership.** `member_ids` and `member_count` of `GET /groups/{id}` and `GET /groups` follow the user-directory rule: the caller plus users whose whole scope lies inside the caller's `user:read` scope. A caller without `user:read` sees only themselves. Unrestricted `user:read` holders are unchanged, and delegated-administration containment is unchanged.
