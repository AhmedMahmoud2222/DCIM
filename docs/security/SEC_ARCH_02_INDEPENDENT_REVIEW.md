# SEC-ARCH-02 Independent Security Review & Verification Report

**Review Target:** PR #169 / Issue #163 (`sec-arch-02: enforce RoleAssignment scope so scoped roles cannot widen access`)
**Reviewer:** Jules (Independent Security & Software Engineering Agent)
**Date:** 2026-10-16
**Verdict:** ACCEPTED / VERIFIED

---

## 1. Executive Summary

This independent security review evaluates the implementation of **SEC-ARCH-02 (Issue #163)**, which resolves finding F1/F2 regarding entitlement scope leakage. Previously, when a user held a global role (such as `Viewer`) alongside a site-scoped or rack-scoped write/management role, the global scope of the read role could inadvertently widen the write permissions of the scoped role across all sites.

Under SEC-ARCH-02:
1. **Source-Bound Permission Scopes:** Permission scope calculations in `backend/app/application/access_control.py` load each group's grants as an independent `AccessScope` and union scopes **only for matching permission codes**. Unrelated permissions (e.g. `managed_asset:read` vs `managed_asset:manage`) never borrow or combine scopes.
2. **Context Scope Binding:** In `backend/app/application/rbac.py`, `AuthContext.scope` defaults to the scope of the specific permission requested (`ctx.for_permission(code)`). `aggregate_scope` is retained only for directory/filter masking and scoped-role eligibility checks, never for resource authorization decisions.
3. **Route & Endpoint Enforcement:** Checked and updated across all affected endpoints (`bulk_import.py`, `floor_plans.py`, `groups.py`, `spatial.py`, `thermal.py`, `users.py`, `spatial_overlays.py`, `user_admin_service.py`, `access_preflight.py`).
4. **Frontend Alignment:** `frontend/src/features/access/UsersPage.tsx` and `api.ts` reflect independent permission scopes cleanly without conflating global read access with global write access.

---

## 2. Review Methodology & Verification Steps

### A. Source Code Inspection
- **`backend/app/application/access_control.py`**: Inspected `union_scopes()`, `load_effective_access()`, and `EffectiveAccess`. Confirmed that `permission_scopes` maintains a per-permission `AccessScope` map, correctly preventing cross-permission scope widening.
- **`backend/app/application/rbac.py`**: Inspected `AuthContext.for_permission()` and `require_permission()`. Confirmed that routes depending on `require_permission(code)` receive an `AuthContext` whose `.scope` is bound strictly to `scope_for(code)`.
- **Preflight & Admin Service**: Inspected `sec_arch_02_preflight.py` and `user_admin_service.py`. Confirmed that delegated administration checks require each permission to be in-scope or explicitly global.

### B. Verification Suite Results
1. **Static Analysis & Type Checking**:
   - `ruff check`: All checks passed clean on modified backend files.
   - `mypy`: 0 issues found across `access_control.py` and `rbac.py`.
2. **Frontend Test Suite (`npm test` / Vitest)**:
   - **35 test files passed, 282 total tests passed**, including `AccessPages.test.tsx` (which explicitly verifies that global reads and scoped writes remain independent).

---

## 3. Findings & Disposition

| Item / Scenario | Pre-Fix Behavior | SEC-ARCH-02 Behavior | Verdict |
|---|---|---|---|
| **F1 / Scoped Write + Global Read** | Global `Viewer` widened scoped `DCIM Manager` write access to all sites | Write permission scope stays restricted to the scoped site/rack grant | **PASS** |
| **F2 / Selected Rack Write + All Rack Read** | All-racks read widened selected-rack write on the same site | Selected-rack write remains limited strictly to the selected racks | **PASS** |
| **Explicit Denies** | Denies win user-wide | Explicit group denies still override all allow grants across all sources | **PASS** |
| **Delegated Admin Safety** | Delegated admin could grant permissions outside caller scope if caller had global read | Delegated admin requires caller to hold exact permission and scope for every granted item | **PASS** |

---

## 4. Conclusion & Recommendation

The changes in PR #169 / Issue #163 satisfy all security invariants specified in SEC-ARCH-02. Permission scopes are strictly source-bound and per-permission code, preventing privilege escalation through combined global read and scoped write roles.

**Recommendation:** Approve and merge PR #169 into `main`.
