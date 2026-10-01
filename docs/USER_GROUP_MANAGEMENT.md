# User & Group Management

Administrator-facing management of users, groups, permissions and site/rack access.
Everything below is enforced on the server; the UI only mirrors it.

## Concepts

| Concept | Meaning |
| --- | --- |
| **Role** (existing) | `RoleAssignment` to a named role (Administrator, DCIM Manager, Engineer, Operator, Viewer). Unchanged by this feature. |
| **Group** (new) | Named set of users carrying permission grants (`allow` / `deny`) and site/rack access. |
| **Site access** | A group grants a site with `rack_scope = all` (every rack currently placed there) or `selected` (only listed racks). |

## Effective permissions

For each request the server computes, from the database (no caching, so changes apply immediately):

1. `allowed` = permissions from the user's roles, plus `allow` grants from every group the user belongs to.
2. `denied` = union of `deny` grants from every group the user belongs to.
3. `effective` = `allowed` minus `denied`.

Rules:

* **Multiple groups: union.** There is no ordering or priority between groups.
* **No inheritance.** Groups do not nest. Nothing is inherited except through the roles a user already holds.
* **Explicit deny wins**, over any allow from any group and over role permissions.
* **Default deny.** A new group grants nothing; a user with no roles and no groups has no access.
* Permission codes are matched exactly (`manage` does not imply `read`), as before.

## Site and rack scope

* A user holding a **global role assignment is unrestricted**. All existing users are in this class, so the migration changes nothing for them.
* Every other user is **site-restricted** and sees only what their groups grant:
  * sites = union of granted sites;
  * for a site, `all` wins over `selected` when two groups disagree; otherwise racks = union of selected racks;
  * a rack is visible only if its **current placement** is in a granted site, so moving a rack out of a site immediately hides it from a stale grant (no cross-site leak);
  * organizations, countries, cities, buildings, floors and rooms are limited to those containing a visible site (cross-tenant isolation);
  * equipment is visible when it sits in a visible rack, or is placed directly in a room of an `all` site;
  * unplaced racks and equipment are invisible to restricted users.
* Out-of-scope objects return **404**, not 403, so ids cannot be probed.
* **Fail closed for unscoped endpoints.** A restricted user keeps only permissions whose endpoints filter by site/rack: `organization:read`, `location:read`, `rack:read`, `rack:manage`, `rack:place`, `equipment:read`, plus user/group administration. Any other granted permission (alarms, telemetry, power, dashboard, imports, ...) is *inactive* for them and reported as such in the effective-access view. Making another module site-aware means adding its permission codes to `SCOPE_AWARE_PERMISSIONS` in `app/application/access_control.py` together with the query filter.
* Restricted users can create a rack only inside an accessible room, and move a rack only between accessible rooms.

## Security safeguards

* **Privilege escalation**
  * An actor can `allow` only permissions they hold; site grants must lie within the actor's own site scope, and rack grants within their visible racks.
  * Adding a user to a group requires holding everything the group confers (permissions and sites).
  * A legacy role can be assigned only if the actor holds all of its permissions, and never by a restricted actor.
  * Users cannot edit or delete a group they belong to, or change their own memberships.
  * **Strict outranking.** Effective authority is the pair (permission set, site/rack scope). Permissions compare by set inclusion and scope by containment (sites, `rack_scope=all`, selected racks), so authority is a partial order and no numeric rank is used. An actor may administer a user only if the user's permissions and scope both lie within the actor's (`target <= actor`) and the actor's do not lie within the user's (`not actor <= target`). Equal authority (peers), wider authority, and incomparable authority are all refused with 403. This covers user update/deactivate/re-password/regroup/delete and every group change that reaches a member (permissions, site access, rename, delete, membership add or remove): the actor must strictly outrank every user the change affects. Consequence: two principals with identical authority, including two global Administrators, cannot administer each other through the API; a strictly more senior principal must do it.
  * Creating a user or granting a group is still delegation: the actor may confer only authority they hold (the grant checks above), which may leave the new principal equal to the actor. The actor cannot administer that principal afterwards.
  * **Resulting authority.** The strict check above runs before the change. Removing a deny (a deny membership, a deny grant, or a whole deny group) can restore allow grants the target already holds elsewhere, so after the change is flushed and before anything commits, every surviving principal it can affect must still satisfy `target <= actor` (equality is allowed, exceeding or becoming incomparable is not). This covers user group replacement, group member add/remove, group permission replacement, group site-access replacement, group deletion and user creation. On failure the whole transaction is rolled back: membership and grant rows, password and refresh-token changes, and the audit row.
  * **No self-membership changes.** Nobody may add themselves to or remove themselves from a group, on either the user route or the group route, because a group can carry deny grants. Changing your own name or password is still allowed.
  * **Concurrency and lock order.** Every authority-changing route (user create/update/delete, group update/delete/members/permissions/site-access) takes the exclusive transaction-scoped advisory lock `dcim.authority_change` first, then re-reads the actor's effective access and re-checks their `user:manage`/`group:manage` permission and active flag. Scope is derived from current rack placement, so the only two writers of rack placement (`placement_service.move_rack` and `retire_rack_placement`, used by the rack create/move/retire routes and the bulk-import rack commit) take the same lock **shared**. A relocation therefore waits for any open authority decision to commit, and an authority decision waits for any open relocation and then reads its result; relocations do not block each other. Global order: `dcim.authority_change` (exclusive or shared) -> `dcim.admin_invariant` -> row locks (import job, rack, placement, group, user). The power-topology and catalog/alarm/discovery locks are never taken in a transaction that also takes the authority lock. A new writer of rack placement, or of room/floor/building parentage, must take the shared lock first (`tests/integration/test_pr67_authority_relocation_race.py` guards the first case). Equipment placement does not enter the authority comparison.
* **Last administrator.** An administrator is an active user holding both `user:manage` and `group:manage`. Any change that would leave none (deny grants, membership, group deletion, deactivation, deletion) fails with 409 and is rolled back. Users cannot deactivate or delete themselves. With strict outranking and the self-membership rule, an actor who may change an administrator also holds both permissions and stays one, so this check is defence in depth rather than a path an authenticated actor can reach today.
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

One behavioural note: a `role_assignment` with a non-global `scope_type` previously granted its permissions globally (Phase 1 decision H7). Such rows cannot be created through the API today. If any exist, the holder is now treated as site-restricted, with the same fail-closed permission filtering.

## Known limitations

* Endpoints outside locations, racks and equipment are not site-scoped yet (see fail-closed rule above).
* The UI creates group-only (site-restricted) users. Legacy role assignment is available on `POST /users` (`role_name`) but has no UI, and roles cannot be changed after creation.
* Deleting a user who owns imports or catalog revisions is refused (409); deactivate instead.
* Effective access is computed per request with a handful of queries; very large tenants may want caching.

## Break-glass and recovery (current state, not implemented here)
`backend/scripts/create_admin.py` is the only out-of-band path. It needs shell access and the application database credentials, creates a *new* global Administrator and refuses (or, with `--if-exists skip`, silently leaves) an existing email. It cannot reset a password, deactivate or demote an existing account, or revoke refresh tokens. It writes no audit entry and needs no approval. Because global Administrators are peers, a compromised or departed Administrator cannot be deactivated through the API by another Administrator, so recovery today means direct database edits. A controlled peer-recovery procedure (separate approvers, an audit record, credential and session revocation) is a separate operational-security follow-up and deliberately not part of this change; no Super Administrator role or numeric rank is introduced.
