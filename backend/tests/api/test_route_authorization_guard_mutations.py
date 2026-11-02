"""Proof that the route-authorization guard detects real authorization regressions (issue #195 / P2 wave 1, guard D).

Each mutation is applied in a throwaway Python process, so the application used by the rest of the suite is never touched,
and the guard's checks then run against it. The control run proves the harness is not vacuous: unmutated, every check is clean.
M1-M8 are the eight mutations from the P2 audit (M1-M5 add a hostile route, M6-M8 weaken the guard of an existing route). M9-M12 are the
duplicate-registration bypasses found in review: FastAPI serves the FIRST route matching a method and path, so a second registration
of an operation must be rejected whichever one comes first and whatever permission codes it carries. M13-M17 close the surfaces the
OpenAPI document cannot show: mounts and websockets (including at documentation paths), a rogue route at a documentation path, and
explicit HEAD/OPTIONS handlers. M13-M15 add no operation, so the operation baseline cannot see them; only `unserved_surface` can.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, sys
from fastapi import Depends
from fastapi.routing import APIRoute
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from app.api.deps import get_current_user
from app.application.rbac import require_permission
from app.main import app
from tests.api.route_authz_guard import check, walk

name = sys.argv[1]

def swap(method, path, code):
    for route in walk(app.routes):
        if getattr(route, "path", None) == path and method in route.methods:
            for sub in route.dependant.dependencies:
                if getattr(sub.call, "__qualname__", "").startswith("require_permission."):
                    sub.call = require_permission(code)
                    return
    raise SystemExit(f"mutation target not found: {method} {path}")

if name == "M1_unauthenticated_route":
    app.add_api_route("/api/v1/rogue/open", lambda: {}, methods=["GET"])
elif name == "M2_authentication_only_route":
    app.add_api_route("/api/v1/rogue/authn", lambda user=Depends(get_current_user): {}, methods=["GET"])
elif name == "M3_write_route_guarded_by_read":
    app.add_api_route("/api/v1/rogue/write", lambda ctx=Depends(require_permission("rack:read")): {}, methods=["POST"])
elif name == "M4_typo_in_permission_code":
    app.add_api_route("/api/v1/rogue/typo", lambda ctx=Depends(require_permission("rack:raed")): {}, methods=["GET"])
elif name == "M5_route_hidden_from_openapi":
    app.add_api_route("/api/v1/rogue/hidden", lambda: {}, methods=["GET"], include_in_schema=False)
elif name == "M6_cable_delete_downgraded":
    swap("DELETE", "/api/v1/cables/{cable_id}", "cable:read")
elif name == "M7_integration_create_downgraded":
    swap("POST", "/api/v1/integrations", "integration:read")
elif name == "M8_pdu_create_downgraded":
    swap("POST", "/api/v1/power/pdus", "power:read")
elif name == "M9_open_route_registered_before_the_protected_one":
    app.router.routes.insert(0, APIRoute("/api/v1/racks", lambda: {"leak": True}, methods=["GET"]))
elif name == "M10_open_route_registered_after_the_protected_one":
    app.router.routes.append(APIRoute("/api/v1/racks", lambda: {"leak": True}, methods=["GET"]))
elif name == "M11_duplicate_with_identical_permission_codes":
    app.router.routes.insert(0, APIRoute("/api/v1/racks", lambda ctx=Depends(require_permission("rack:read")): {}, methods=["GET"]))
elif name == "M12_duplicate_with_a_renamed_path_parameter":
    app.router.routes.insert(0, APIRoute("/api/v1/racks/{rack_identifier}", lambda: {"leak": True}, methods=["GET"]))
elif name in ("M13_rogue_mount_at_a_docs_path", "M14_rogue_route_at_a_docs_path", "M15_websocket_at_a_docs_path"):
    async def secret(request):
        return JSONResponse({"leak": True})
    async def socket(websocket):
        await websocket.close()
    if name.startswith("M13"):
        app.router.routes.insert(0, Mount("/docs", app=Starlette(routes=[Route("/secret", secret)])))
    elif name.startswith("M14"):
        app.router.routes.insert(0, Route("/docs", secret))
    else:
        app.router.routes.insert(0, WebSocketRoute("/redoc", socket))
elif name == "M16_unauthenticated_options_handler":
    app.add_api_route("/api/v1/rogue/options", lambda: {"leak": True}, methods=["OPTIONS"])
elif name == "M17_unauthenticated_head_handler":
    app.add_api_route("/api/v1/rogue/head", lambda: {"leak": True}, methods=["HEAD"])
elif name != "CONTROL":
    raise SystemExit("unknown mutation " + name)
print(json.dumps({k: len(v) for k, v in check(app).items()}))
"""

# mutation -> the dedicated checks that must report it (the baseline comparison must always report every mutation)
MUTATIONS = {
    "M1_unauthenticated_route": {"public_routes"},
    "M2_authentication_only_route": {"manual_routes"},
    "M3_write_route_guarded_by_read": {"write_with_read_permission"},
    "M4_typo_in_permission_code": {"codes_are_recognised"},
    "M5_route_hidden_from_openapi": {"unserved_surface", "traversal_matches_openapi"},
    "M6_cable_delete_downgraded": {"write_with_read_permission"},
    "M7_integration_create_downgraded": {"write_with_read_permission"},
    "M8_pdu_create_downgraded": {"write_with_read_permission"},
    "M9_open_route_registered_before_the_protected_one": {"duplicate_operations"},
    "M10_open_route_registered_after_the_protected_one": {"duplicate_operations"},
    "M11_duplicate_with_identical_permission_codes": {"duplicate_operations"},
    "M12_duplicate_with_a_renamed_path_parameter": {"duplicate_operations"},
    "M13_rogue_mount_at_a_docs_path": {"unserved_surface"},
    "M14_rogue_route_at_a_docs_path": {"unserved_surface"},
    "M15_websocket_at_a_docs_path": {"unserved_surface"},
    "M16_unauthenticated_options_handler": {"public_routes"},
    "M17_unauthenticated_head_handler": {"public_routes"},
}
# mutations that add no operation, so the operation baseline is expected to stay unchanged
BASELINE_BLIND = {"M13_rogue_mount_at_a_docs_path", "M14_rogue_route_at_a_docs_path", "M15_websocket_at_a_docs_path"}


def run(name: str) -> dict[str, int]:
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, name], cwd=BACKEND, env=os.environ.copy(), capture_output=True, text=True, timeout=180, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_control_run_without_a_mutation_is_clean():
    assert {k: v for k, v in run("CONTROL").items() if v} == {}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_guard_detects_the_mutation(name):
    result = run(name)
    if name not in BASELINE_BLIND:
        assert result["baseline_drift"] > 0, f"{name}: the baseline comparison must always notice a changed authorization surface"
    missing = {c for c in MUTATIONS[name] if result[c] == 0}
    assert not missing, f"{name}: the dedicated checks {sorted(missing)} did not fire ({result})"


def test_built_in_documentation_handlers_are_accepted_and_nothing_else_is_exempt():
    """Positive control: the four FastAPI documentation handlers pass every check, so the M13-M15 detections are not blanket rejections."""
    from app.main import app
    from tests.api.route_authz_guard import DOCS_PATHS, check, is_builtin_docs, walk

    docs = {r.path for r in walk(app.routes) if is_builtin_docs(r)}
    assert docs == DOCS_PATHS
    assert check(app)["unserved_surface"] == []
