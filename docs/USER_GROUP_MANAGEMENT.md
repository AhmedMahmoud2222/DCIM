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

- Use `--output`. On POSIX the file is created with owner-only mode (`0600`); on Windows that mode is not an ACL, so the directory ACL below is the control. The command fails if the path already exists or is a symlink; it never overwrites. Without `--output` the report goes to stdout with a warning on stderr, so avoid it in shared terminals, CI logs and pipelines that are captured.
- Never upload the report as a CI artifact, attach it to an issue or PR, or commit it (`sec-arch-02-preflight*.json` is git-ignored as a safety net). Share it only over an access-controlled channel with the owner.
- Delete it after the rollout decision is recorded; keep only a sanitized summary (counts, no IDs).
- On failure the command prints only the exception class name, so errors cannot leak connection details or report content. Run it with debug tooling only on an authorized host.

#### Confidentiality

The report is an access map: user IDs plus permission codes, grant-source names and IDs, raw site/rack scopes, latent grants and the surviving administrator set. It does not contain passwords or tokens, but it is still confidential and must not be pasted into tickets, chat, email, an AI assistant or a public review tool; those are publication channels. Do not screenshot it, redirect stdout into a shared log, enable a PowerShell transcript, or leave it open in an editor that syncs to a cloud service. Transfer it only over an owner-approved access-controlled channel, and give it only to the owner and the named reviewer. If it is exposed, stop using it, notify the owner, remove every copy you can identify and record the exposure in the rollout record.

#### NTFS ACL trust boundary (Windows)

On Windows the report directory is the security boundary, not the file's POSIX mode. The directory is limited to the authorized operator identity, the service identity that runs the preflight (if it is not the operator), `BUILTIN\Administrators` (`*S-1-5-32-544`) and `NT AUTHORITY\SYSTEM` (`*S-1-5-18`). No other principal may hold **any** access: not read, write, modify, delete, delete-child, change-permissions or take-ownership. That includes `Authenticated Users`, `Users`, `INTERACTIVE`, the CI runner account, a shared team account, an unrelated service account and any cloud-sync or backup agent. If the preflight runs as a built-in service account such as `LOCAL SERVICE`, that account is the service identity; do not share it with unrelated services. Administrators and SYSTEM are highly privileged and can override ACLs anyway; the rule keeps the directory from adding a wider path.

Use SIDs as above when documenting or scripting the grant, because built-in account names are localized. Remove unwanted grants rather than adding a deny for a broad group: a deny for a broad group also applies to trusted members of that group and is hard to reason about alongside inherited ACEs.

Inheritance is part of the boundary. An ACE inherited from a parent can grant access even when the directory has restrictive explicit ACEs, and a new report file inherits the directory's inheritable ACEs. Disable inheritance on the report directory, then grant only the trusted identities with object and container inherit flags `(OI)(CI)` so new report files receive the same boundary. A file created before remediation keeps its old inherited ACEs; reset its ACL too, or delete it and generate a new report.

The procedure below is a template. Replace `<report-directory>`, `<report-file>`, `<operator-identity>`, `<reviewer-identity>`, `<service-identity>`, `<untrusted-identity>` and `<acl-backup-file>` with local values. It assumes no fixed Windows path, drive letter or user name.

#### Read-only inspection

These commands only read security descriptors; they do not change ACLs. During inspection use only the read-only forms shown here; do not use `icacls` with `/grant`, `/deny`, `/remove`, `/inheritance`, `/setowner` or `/reset`. Do not run inspection from a shared or untrusted account.

```powershell
$reportDir = '<report-directory>'
$reportFile = Join-Path $reportDir '<report-file>'

# ACEs on the directory and the file, including inherited entries marked (I).
icacls $reportDir
icacls $reportFile

# Structured view: owner, inheritance state and every ACE.
Get-Acl $reportDir | Select-Object Owner, AreAccessRulesProtected
(Get-Acl $reportDir).Access |
  Select-Object IdentityReference, FileSystemRights, AccessControlType,
                IsInherited, InheritanceFlags, PropagationFlags |
  Format-Table -AutoSize

(Get-Acl $reportFile).Access |
  Select-Object IdentityReference, FileSystemRights, AccessControlType,
                IsInherited, InheritanceFlags, PropagationFlags |
  Format-Table -AutoSize

# Check every ancestor, because a grant can be inherited from above.
$current = Get-Item $reportDir
while ($null -ne $current) {
  icacls $current.FullName
  $current = $current.Parent
}
```

`AreAccessRulesProtected = True` means inheritance is disabled on that object; `False` means it still inherits. `icacls` marks inherited ACEs with `(I)`; explicit ACEs have no `(I)`. To search an ACL for a known SID, use the read-only `icacls "<report-directory>" /findsid <SID> /T /C`. `Get-Acl` and `icacls` list ACEs but do not compute a single effective-rights answer. Effective access is not "allows unless any deny matches". Windows evaluates the DACL in order for the specific rights requested: it walks the ACEs that match the caller's user SID or group SIDs, a matching deny that covers a requested right not yet granted refuses the request, and a matching allow grants only the rights it names. Rights already granted by an earlier ACE are not removed by a later deny. Explicit ACEs normally precede inherited ones, but a hand-built DACL can be out of canonical order, and token privileges (for example backup, restore and take-ownership) can bypass the DACL for privileged callers. Do not hand-compute this for a principal. Instead, per principal and per right (read data, write data, add file, delete, delete child, change permissions, take ownership), use a Windows effective-access mechanism: the Effective Access tab in the folder's Advanced Security Settings, or, on an approved host with signed Sysinternals tools, `accesschk.exe -d -w "<report-directory>"` and `accesschk.exe -d -r "<report-directory>"` with an explicit account argument (`accesschk.exe <account> -d -w "<report-directory>"`), so each evaluated principal is named. Use `whoami /user` and `whoami /groups` to see a token you control. Record which principal and which rights were evaluated, and treat the output as confidential too.

Review the report file separately from its directory. A parent can grant `DELETE_CHILD` (`DC`) while the file itself is read-only, and an explicit file ACE can override the directory for that file. Rights that matter in the output:

| Right | Meaning |
| --- | --- |
| `(F)` | Full control, including change-permissions and take-ownership. |
| `(M)` | Modify, including delete. |
| `(W)` | Write data or add files. |
| `(D)`, `(DE)` | Delete the object. |
| `(DC)` | Delete child; on the directory this can allow deleting the report file. |
| `(WDAC)` | Change permissions (write DAC). |
| `(WO)` | Take ownership (write owner). |

Any of these held by an identity outside the trust boundary is a finding. So is `(I)` on an untrusted ACE, or `AreAccessRulesProtected = False` with a permissive parent.

The owner is part of the boundary. The owner of an NTFS object can rewrite its DACL (implicit `READ_CONTROL` and `WRITE_DAC`) even when no `WDAC` ACE names the owner, so an untrusted owner defeats an otherwise clean ACL. Read `Owner` for both `$reportDir` and `$reportFile` (`(Get-Acl $reportFile).Owner`) and treat any owner other than the operator identity, the service identity, `BUILTIN\Administrators` or `NT AUTHORITY\SYSTEM` as a finding. Owner changes are a privileged remediation step, not part of inspection: have an authorized administrator set the owner (`icacls "<report-directory>" /setowner "*S-1-5-32-544" /T /C`, or the operator identity) and re-inspect. If the owner cannot be corrected, delete the file and regenerate it in a directory with a trusted owner. Recheck the owner after any restore.

#### Remediation

Only an authorized operator changes these ACLs, and only after recording the before state and the owner's approval. Change one directory first, verify it, and only then handle existing report files. Export the current DACL to a confidential backup so the change can be reversed:

```powershell
icacls "<report-directory>" /save "<acl-backup-file>" /t /c
```

The backup contains security descriptors; protect and delete it like the report. `icacls /save` stores names relative to the directory that contains the saved item, and `/restore` requires an explicit directory operand that is that same base. Pair them with `<report-parent>`, the directory containing `<report-directory>`: save with `icacls "<report-directory>" /save "<acl-backup-file>" /t /c` (as above) and restore with `icacls "<report-parent>" /restore "<acl-backup-file>" /c`. Do not rely on the working directory. Before depending on this pair, validate it once on a throwaway directory tree on the target Windows version (save, change an ACL, restore, compare `icacls` output). `/restore` reapplies the saved descriptors, including the old broad grants and owner, so restore only to reverse a failed change, then re-run the inspection. Keep the backup outside the report directory.

```powershell
# 1. Stop inheriting permissive ACEs from the parent.
icacls "<report-directory>" /inheritance:r

# 2. Grant only the trust boundary. The operator needs modify (M) to create
#    reports and to delete them later; the service identity needs M if it writes.
icacls "<report-directory>" /grant:r "<operator-identity>:(OI)(CI)M"
icacls "<report-directory>" /grant:r "<service-identity>:(OI)(CI)M"
icacls "<report-directory>" /grant:r "*S-1-5-32-544:(OI)(CI)F"
icacls "<report-directory>" /grant:r "*S-1-5-18:(OI)(CI)F"

# 3. Remove every other explicit grant. Repeat for each identity found above.
icacls "<report-directory>" /remove:g "<untrusted-identity>"
```

`/remove:g` removes grants and `/remove:d` removes denies. Do not remove an identity unless you have identified it; if an ACE names an unknown SID, resolve it or stop. An existing report file may carry explicit ACEs for untrusted identities, which `/inheritance:r` keeps and `/grant:r` does not touch. The safest course is to delete the file and generate a new report in the remediated directory, where it inherits only the trust boundary. Only if the file must be kept, reset it to a known state and then verify it:

```powershell
# Remove inherited ACEs, then explicitly remove every untrusted explicit grant
# (and deny) found by the inspection. Repeat per identified identity.
icacls "<report-file>" /inheritance:r
icacls "<report-file>" /remove:g "<untrusted-identity>"
icacls "<report-file>" /remove:d "<untrusted-identity>"

# Grant only the trust boundary.
icacls "<report-file>" /grant:r "<reviewer-identity>:R"
icacls "<report-file>" /grant:r "*S-1-5-32-544:F"
icacls "<report-file>" /grant:r "*S-1-5-18:F"
```

Re-inspect the file: its ACL must list only the identities above, with no unidentified SID, and its owner must be trusted. If any untrusted ACE or owner remains, delete the file and regenerate it rather than iterating.

Deletion still works through the parent directory's `DC` right; if it does not, grant `M` to the operator on the file rather than widening the directory. Re-run the inspection after every change and confirm `AreAccessRulesProtected = True`, no untrusted ACE remains on the directory or file, the directory and file owners are trusted, and no ancestor grants an untrusted principal a right that could delete, replace, rename or control the report directory.

Ancestors are judged differently from the report directory. Ordinary traverse, list and read rights on shared ancestors (for example a volume root readable by `Users`) are expected and do not expose a report in a directory whose own DACL is protected, so they are not a finding. These ancestor rights are findings for an untrusted principal: `DELETE` on the report directory, `DELETE_CHILD` on its immediate parent (this lets the principal remove or replace the directory), `WRITE_DATA`/`ADD_FILE`/`ADD_SUBDIRECTORY` on the parent (this lets the principal plant a replacement or a junction), `WRITE_DAC`, `WRITE_OWNER`, or ownership of the parent or of any ancestor that can be renamed. Also consider ancestors above the parent: rename or delete rights there can move the whole path. Prefer a dedicated report directory directly under a trusted location so the ancestor chain is short. If you cannot prove all of this, do not create the report; stop and escalate. Record the approver, timestamp and before/after ACL evidence in the rollout record, redacting account names if that record is more widely readable than the report.

If the target is FAT/exFAT, a network share, a cloud-synced folder such as OneDrive or Dropbox, a mounted container or WSL path, or any non-NTFS volume, do not treat `icacls` output as authoritative and do not write the report there. Choose a local NTFS directory on an approved encrypted volume. Full-disk encryption and EFS complement ACLs but do not replace them.

#### What the writer does and does not enforce

`O_EXCL` only refuses to create over an existing name, including a link planted at that name; `O_NOFOLLOW` only refuses a symbolic link as the final path component on platforms that support it; the `0o600` argument is a POSIX mode request. None of them restrict which identities can open the file after creation, and on Windows none of them produce an NTFS ACL: the file receives the ACEs it inherits from its directory, subject to their inheritance flags.

The Windows branch of the directory check (`os.name == "nt"` in `_validate_directory_security`, called by `write_report_file`) performs **no ACL inspection**. It does not read the DACL, owner, inherited ACEs or modify/delete rights, and it does not fail closed when the directory is broadly accessible. The POSIX branch checks owner and group/world write; the Windows branch is a deliberate no-op. Do not describe a Windows run as "owner-only" or "secure directory validated" on the strength of the writer. The operator's ACL procedure above is the only control on Windows.

#### File lifecycle and retention

- Use one dedicated report directory and one unique report file per run. The writer already refuses to overwrite; do not reuse a name.
- Keep the report only until the rollout decision is recorded. Delete it then, on abort, or when it is superseded, and leave only a sanitized summary containing counts and no user IDs, source names or scopes.
- The writer removes an incomplete file when creation or writing fails after the file exists, but verify the directory afterward and remove any leftover fragment yourself.
- Deleting a file removes its directory entry; it is not a guarantee of physical erasure. Rely on full-disk encryption for the volume and the owner's media-sanitization procedure for retired disks.
- If the report directory is captured by a backup or snapshot, that copy inherits the backup's retention. Exclude the directory from backups where policy allows, or protect the backup as confidential and delete both copies when retention ends.
- Re-check the ACL after any operator, service-account or role change, and remove the operator's access when they leave the rollout.

#### Keeping reports out of Git and CI artifacts

`sec-arch-02-preflight*.json` in `.gitignore` is a safety net, not a control. Before staging anything, run `git status --porcelain` and confirm no report appears; never use `git add -f` for it. Do not attach a report to an issue, pull request, review comment or chat, and do not paste its content into a commit message. Those channels are retained and may be visible beyond the rollout team.

CI logs, test reports, coverage files, caches and uploaded artifacts are also publication channels. Do not run the report-producing preflight in CI, do not point `--output` at a path inside a checkout or workspace, and do not add a report path to an artifact upload, cache key, test-result directory or `tee`/`Out-File` target. If a future authorized workflow needs preflight output, it needs a separate reviewed change with a runner-temp path outside the workspace, restrictive permissions and no artifact upload; this document does not authorize that change. If a report is committed or uploaded by mistake, treat it as an exposure: notify the owner, remove the artifact, and let the owner decide whether history rewriting is required. Deleting it in a later commit does not remove it from history, forks or caches.

#### Residual risk: same-user directory mutation

This record accepts one residual risk for the LOW follow-up. The ACL model treats the operator account as a single trust domain. Any process running as that same user — another terminal, an editor, a sync client, a compromised tool or an agent — can read the report, change it, delete it, replace the report directory, or race the path between the directory check and file creation. ACLs cannot separate processes that share a user context. Because the Windows branch performs no ACL validation, a same-user attacker can also leave the boundary looking correct.

`O_EXCL`, `O_NOFOLLOW` and `0600` do not reduce this risk, and neither does this procedure. Compensating controls are a dedicated non-shared operator account, no shared service accounts, MFA and endpoint protection on the host, the shortest practical retention, and hashing the report at creation with `Get-FileHash -Algorithm SHA256` and comparing it before use. A hash detects accidental change; it cannot stop a same-user attacker who can rewrite both the report and the hash. Review the ACLs after any account or role change. This residual risk is accepted for this documentation-only follow-up and does not request or authorize a code change; if the owner requires technical ACL enforcement in the writer, that is a separate product decision and must be raised before implementation.

Validate both fresh and populated PostgreSQL states, then require independent exact-HEAD Jules acceptance and all required CI before merge. Deployment and production grant changes require separate owner authorization. Reverting to the flattened evaluator reintroduces F1/F2; use a reviewed forward fix or suspend affected operations instead of silently restoring that behavior.

Some descriptions above retain SEC-ARCH-01's historical terminology for role eligibility. For **group composition and resource enforcement**, this section and the permission-local rules at the top supersede the previous user-wide union/global-override contract. Site-role admission, organization-role admission, building removal, deny-all router restrictions, and role-name checks remain unchanged.

## Route authorization guard and directory masking (issue #195, P2 wave 1)
* **Route guard.** `backend/tests/api/test_route_authorization_inventory.py` derives, from the FastAPI app, which control guards each of the API operations (permission code, permission plus Administrator role, handler-authorized, collector HMAC, public) and requires the result to equal the reviewed `backend/tests/api/route_authz_baseline.py`. A new, removed or re-guarded route fails the backend CI job until that file is edited in the same pull request, so the change is a reviewable diff. Unauthenticated (`PUBLIC`) and handler-authorized (`MANUAL`) routes need an allowlist entry with a reason; `MANUAL` entries must name an existing test. State-changing routes may not be guarded only by a read-type permission (three documented dry-run exceptions), permission codes must be seeded, and nothing may be served that the guard cannot classify (hidden OpenAPI entries, mounts, websockets). `test_route_authorization_guard_mutations.py` proves the guard fails on the eight audit mutations plus four duplicate-registration bypasses (FastAPI serves the first route for a method and path, so any second registration of an operation is rejected, in either order and whatever permission codes it carries). The guard reads FastAPI's private include-routing objects (observed in 0.156.x); that dependency is isolated in `route_authz_guard.py` and a contract check fails loudly if FastAPI changes it.
* **Cable neighbors.** `POST /cables/from-neighbor/{id}` checks that both resolved ports are visible to the caller before any state check. A neighbor with a port outside the caller's scope answers exactly like a missing neighbor (404 naming only the supplied id); its state and its ports are never observable.
* **Group membership.** `member_ids` and `member_count` of `GET /groups/{id}` and `GET /groups` follow the user-directory rule: the caller plus users whose whole scope lies inside the caller's `user:read` scope. A caller without `user:read` sees only themselves. Unrestricted `user:read` holders are unchanged, and delegated-administration containment is unchanged.
