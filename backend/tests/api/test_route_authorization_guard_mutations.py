"""Proof that the route-authorization guard detects real authorization regressions (issue #195 / P2 wave 1, guard D).

Each mutation is applied in a throwaway Python process, so the application used by the rest of the suite is never touched,
and the guard's checks then run against it. The control run proves the harness is not vacuous: unmutated, every check is clean.
The eight mutations are the ones from the P2 audit: M1-M5 add a hostile route, M6-M8 weaken the guard of an existing route.
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
}


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
    assert result["baseline_drift"] > 0, f"{name}: the baseline comparison must always notice a changed authorization surface"
    missing = {c for c in MUTATIONS[name] if result[c] == 0}
    assert not missing, f"{name}: the dedicated checks {sorted(missing)} did not fire ({result})"
