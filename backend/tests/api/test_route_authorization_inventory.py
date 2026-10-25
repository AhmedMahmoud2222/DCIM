"""Route-authorization regression guard (issue #195 / P2 wave 1, guard D). Static: no database, no HTTP.

A new or changed endpoint cannot ship without an explicit, reviewable authorization decision:
the live inventory must equal tests/api/route_authz_baseline.py, unauthenticated and handler-authorized routes must be
allowlisted with a reason, write routes may not hide behind read permissions, and nothing may be served that the guard cannot
inspect. See route_authz_guard.py for the FastAPI-internals contract and
tests/api/test_route_authorization_guard_mutations.py for proof that the guard fails on real authorization regressions.
"""

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI

from app.application.rbac import require_permission
from app.main import app
from tests.api.route_authz_baseline import BASELINE, MANUAL, PUBLIC
from tests.api.route_authz_guard import check, classify, find_duplicates

BACKEND = Path(__file__).resolve().parents[2]
RESULTS = check(app)


def assert_clean(name: str, hint: str) -> None:
    assert not RESULTS[name], f"{hint}\n  " + "\n  ".join(RESULTS[name])


def test_the_traversal_of_fastapi_internals_is_trusted_for_this_fastapi_version():
    assert_clean("fastapi_contract", "FastAPI's private routing API changed; update tests/api/route_authz_guard.py:")


def test_traversal_sees_exactly_the_operations_the_public_openapi_document_lists():
    assert_clean("traversal_matches_openapi", "the guard would inspect less (or more) than the API serves:")


def test_the_classified_inventory_equals_the_reviewed_baseline():
    assert_clean(
        "baseline_drift",
        "The authorization surface changed. Review every line, then update tests/api/route_authz_baseline.py "
        "(a new route needs its permission code, or an allowlist entry with a reason):",
    )


def test_unauthenticated_routes_are_exactly_the_justified_allowlist():
    assert_clean("public_routes", "routes with no authentication must be in PUBLIC with a reason:")
    assert all(PUBLIC.values())


def test_handler_authorized_routes_are_exactly_the_justified_allowlist():
    assert_clean("manual_routes", "handler-authorized routes must be in MANUAL with a reason and an enforcement test:")


@pytest.mark.parametrize("op", sorted(MANUAL), ids=lambda op: f"{op[0]} {op[1]}")
def test_every_manual_route_names_an_existing_enforcement_test(op):
    reason, test_file = MANUAL[op]
    assert reason and (BACKEND / test_file).is_file(), (op, test_file)


def test_every_guarding_permission_code_is_seeded():
    assert_clean("codes_are_recognised", "routes reference permission codes no seeded role holds (typo or unseeded):")


def test_state_changing_routes_are_not_guarded_only_by_read_permissions():
    assert_clean(
        "write_with_read_permission",
        "a non-GET route is guarded by a read-type permission (add to READ_ONLY_POSTS only if it never mutates):",
    )


def test_nothing_is_served_outside_the_inspected_router_tree():
    assert_clean("unserved_surface", "routes/mounts/websockets the guard cannot classify, or routes hidden from OpenAPI:")


def test_baseline_and_allowlists_are_internally_consistent():
    live = classify(app)
    assert set(PUBLIC) <= set(BASELINE) and set(MANUAL) <= set(BASELINE)
    assert {op for op, (kind, _c) in BASELINE.items() if kind == "public"} == set(PUBLIC)
    assert {op for op, (kind, _c) in BASELINE.items() if kind == "manual"} == set(MANUAL)
    assert len(live) == len(BASELINE)


def test_no_operation_is_registered_twice():
    assert_clean("duplicate_operations", "FastAPI serves only the first of two routes with the same method and path; remove the duplicate:")


def mini_app(*registrations) -> FastAPI:
    mini = FastAPI()
    for path, method, guarded in registrations:
        if guarded:
            mini.add_api_route(path, lambda ctx=Depends(require_permission("rack:read")): {}, methods=[method])
        else:
            mini.add_api_route(path, lambda: {}, methods=[method])
    return mini


@pytest.mark.parametrize(
    "registrations",
    [
        [("/x", "GET", False), ("/x", "GET", True)],  # unguarded first, protected second
        [("/x", "GET", True), ("/x", "GET", False)],  # protected first, unguarded second
        [("/x", "GET", True), ("/x", "GET", True)],  # identical permission codes
        [("/x/{a}", "GET", True), ("/x/{b}", "GET", False)],  # same shape, renamed path parameter
    ],
    ids=["open-first", "open-last", "identical-codes", "renamed-parameter"],
)
def test_duplicate_registrations_are_found_regardless_of_order_or_codes(registrations):
    assert find_duplicates(mini_app(*registrations))


def test_distinct_operations_are_not_duplicates():
    legitimate = mini_app(("/x", "GET", True), ("/x", "POST", True), ("/x/{a}", "GET", True), ("/x/compare", "GET", True))
    assert find_duplicates(legitimate) == []