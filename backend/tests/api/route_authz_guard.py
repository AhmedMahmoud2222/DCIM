"""Deterministic route-authorization inventory (issue #195 / P2 wave 1, guard D).

Derives, from the FastAPI application itself, which authorization control guards every API operation, and checks that
inventory against the reviewed baseline in route_authz_baseline.py. It needs no database and no HTTP calls.

FastAPI-internals dependency (deliberate, isolated here, fail-closed):
  * FastAPI < 0.15x flattens `include_router` into plain `APIRoute` objects in `app.routes`.
  * FastAPI >= 0.15x keeps lazy `_IncludedRouter` nodes whose `effective_candidates()` yield `_EffectiveRouteContext`
    objects (private API, observed in 0.156.x). Both expose `.path`, `.methods`, `.dependant`, `.include_in_schema`.
  The public `app.openapi()` document cannot show permission codes, so the dependency tree is walked. `fastapi_contract_problems`
  and the `traversal_matches_openapi` check make any upstream change fail loudly instead of letting the guard inspect less than
  is served. Permission codes are read from the closure of `require_permission` / `require_catalog_administrator`
  (app/application/rbac.py): renaming their `code` variable is caught by `codes_are_recognised`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fastapi
from fastapi.routing import APIRoute

from app.application.rbac import DEFAULT_ROLE_PERMISSIONS
from tests.api.route_authz_baseline import BASELINE, MANUAL, PUBLIC, READ_ONLY_POSTS

HTTP = ("GET", "POST", "PUT", "PATCH", "DELETE")
READ_ACTIONS = {"read", "view", "download", "read_draft", "document_download"}
DOCS_PATHS = {"/api/v1/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
PERMISSION_FACTORIES = ("require_permission.", "require_catalog_administrator.")

Classified = dict[tuple[str, str], tuple[str, tuple[str, ...]]]


def _is_operation(route: Any) -> bool:
    return hasattr(route, "dependant") and hasattr(route, "methods") and hasattr(route, "include_in_schema")


def walk(routes: Any) -> Iterator[Any]:
    """Every effective route, whichever FastAPI include strategy is in use."""
    for route in routes:
        if isinstance(route, APIRoute) or _is_operation(route):
            yield route
        elif hasattr(route, "effective_candidates"):
            yield from walk(route.effective_candidates())
        else:
            yield route  # not classifiable: reported by `unserved_surface`


def dependency_calls(dependant: Any) -> Iterator[Any]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from dependency_calls(sub)


def permission_codes(call: Any) -> tuple[str, ...]:
    qualname = getattr(call, "__qualname__", "")
    if not qualname.startswith(PERMISSION_FACTORIES):
        return ()
    names = getattr(getattr(call, "__code__", None), "co_freevars", ())
    cells = getattr(call, "__closure__", None) or ()
    return tuple(cell.cell_contents for name, cell in zip(names, cells, strict=True) if name == "code" and isinstance(cell.cell_contents, str))


def fastapi_contract_problems(app: Any) -> list[str]:
    """Reasons the traversal cannot be trusted with this FastAPI version (empty = trusted)."""
    problems = []
    routes = list(walk(app.routes))
    if not any(_is_operation(r) for r in routes):
        problems.append(f"no classifiable routes found with FastAPI {fastapi.__version__}")
    lazy = [r for r in app.routes if hasattr(r, "effective_candidates")]
    if lazy and not any(_is_operation(r) for r in walk([lazy[0]])):
        problems.append("lazy include nodes yielded no operations: FastAPI private routing API changed")
    return problems


def classify(app: Any) -> Classified:
    result: Classified = {}
    for route in walk(app.routes):
        if not _is_operation(route) or route.dependant is None:
            continue
        calls = list(dependency_calls(route.dependant))
        codes = tuple(sorted({code for call in calls for code in permission_codes(call)}))
        qualnames = {getattr(c, "__qualname__", type(c).__name__) for c in calls}
        if codes:
            admin = any(getattr(c, "__qualname__", "").startswith("require_catalog_administrator.") for c in calls)
            kind = "permission+administrator" if admin else "permission"
        elif "get_current_collector" in qualnames:
            kind = "collector_hmac"
        elif qualnames & {"get_auth_context", "get_current_user"}:
            kind = "manual"
        else:
            kind = "public"
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            result[(method, route.path)] = (kind, codes)
    return result


def check(app: Any) -> dict[str, list[str]]:
    """Run every guard check; each maps to a list of violations (empty = passed)."""
    live = classify(app)
    listed = {(m.upper(), p) for p, item in app.openapi()["paths"].items() for m in item if m.upper() in HTTP}
    known_codes = set(DEFAULT_ROLE_PERMISSIONS["Administrator"])
    operations = [r for r in walk(app.routes) if _is_operation(r)]
    stray = [getattr(r, "path", repr(r)) for r in walk(app.routes) if not _is_operation(r) and getattr(r, "path", None) not in DOCS_PATHS]
    hidden = [r.path for r in operations if not r.include_in_schema]
    public = {op for op, (kind, _c) in live.items() if kind == "public"}
    manual = {op for op, (kind, _c) in live.items() if kind == "manual"}
    return {
        "fastapi_contract": fastapi_contract_problems(app),
        "traversal_matches_openapi": [f"{op}" for op in sorted(set(live) ^ listed)],
        "baseline_drift": [
            *(f"new route {op} -> {live[op]}" for op in sorted(set(live) - set(BASELINE))),
            *(f"removed route {op}" for op in sorted(set(BASELINE) - set(live))),
            *(f"guard changed {op}: {BASELINE[op]} -> {live[op]}" for op in sorted(set(live) & set(BASELINE)) if live[op] != BASELINE[op]),
        ],
        "public_routes": [
            *(f"unexpected unauthenticated route {op}" for op in sorted(public - set(PUBLIC))),
            *(f"allowlisted public route missing {op}" for op in sorted(set(PUBLIC) - public)),
        ],
        "manual_routes": [
            *(f"unexpected handler-authorized route {op}" for op in sorted(manual - set(MANUAL))),
            *(f"allowlisted manual route missing {op}" for op in sorted(set(MANUAL) - manual)),
        ],
        "codes_are_recognised": [
            f"{op} uses unseeded permission code {code}"
            for op, (_k, codes) in sorted(live.items()) for code in codes if code not in known_codes
        ],
        "write_with_read_permission": [
            f"{op} changes state but is guarded only by {codes}"
            for op, (_k, codes) in sorted(live.items())
            if op[0] != "GET" and codes and all(c.split(":")[1] in READ_ACTIONS for c in codes) and op not in READ_ONLY_POSTS
        ],
        "unserved_surface": [*(f"unclassifiable route/mount {p}" for p in stray), *(f"hidden from OpenAPI {p}" for p in hidden)],
    }
