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
  * An actor cannot modify (deactivate, re-password, regroup, delete) a user who holds permissions the actor lacks, and restricted actors cannot administer unrestricted users.
* **Last administrator.** An administrator is an active user holding both `user:manage` and `group:manage`. Any change that would leave none (deny grants, membership, group deletion, deactivation, deletion) fails with 409 and is rolled back. Users cannot deactivate or delete themselves.
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

## Import jobs and site-restricted users (interim rule, product decision pending)
Operations (bulk-import pipeline): upload and template download per domain (`POST|GET /racks|equipment|catalog/import-*`), and on `/import-jobs/{id}`: status, `/rows` (preview, `?status=invalid` for validation errors), `/report`, `/commit`, `/cancel`. There is no list or retry endpoint.

Current behaviour: a site-restricted caller may touch a job only if they uploaded it; any other job answers a 404 identical to a missing id, for every operation. Unrestricted callers keep the earlier behaviour (a holder of `rack:read` / `equipment:read` can read any job of that type; `catalog` jobs need `catalog:read`).

What the acceptance tests show (`tests/api/test_pr68_import_job_acceptance.py`):
* `rack:import`, `equipment:import` and `catalog:import` are not site-aware, so they are inactive for a restricted user. A restricted user therefore cannot upload, commit or cancel, and the only jobs they can "own" are ones uploaded while they were unrestricted. Today that needs a direct database change (roles cannot change through the API).
* Uploader ownership alone does not equal scope: a historical owner keeps read access to a job whose rows describe rooms outside their current scope. The tests pin this behaviour; they do not endorse it.
* No site or rack link has been added to jobs and the scope model is unchanged.

Decision needed before import is made available to restricted users (affected operations in brackets): (a) deny all import-job reads to restricted callers regardless of ownership [status, rows, report]; (b) keep uploader-only; (c) link jobs to a site and filter rows and reports by scope, which also needs scope checks in the row validators and commit, because they resolve rooms and racks by code without consulting the caller's scope [all operations, including upload and commit]. Until (c) exists, `*:import` must stay outside `SCOPE_AWARE_PERMISSIONS`.
