"""Tests for the fail-closed frontend audit gate (.github/scripts/frontend_audit_gate.py).

SECURITY EXCEPTION NOT APPROVED OR ACTIVE. These tests prove two things:

1. The mechanism fails closed for every malformed, ambiguous, expired, unreachable or
   out-of-scope input, and passes only for the narrow, fully valid case.
2. The mechanism is inactive in this change: the shipped approval record is unapproved and no
   workflow calls the wrapper, so the existing `npm audit --audit-level=high` gate is unchanged.

The configurations written below are TEST FIXTURES. They are not an owner approval.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml

from ci_support import CI_YML, ROOT, SCRIPTS, load_module

gate = load_module("frontend_audit_gate", SCRIPTS / "frontend_audit_gate.py")

FIXTURES = ROOT / "tests" / "ci" / "fixtures" / "frontend_audit_gate"
SHIPPED_CONFIG = ROOT / ".github" / "security-exceptions" / "frontend-audit-picomatch.json"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
EXC = "GHSA-fc5q-39xc-vjpf"
OTHER_HIGH = "GHSA-2222-3333-4444"
OTHER_CRITICAL = "GHSA-5555-6666-7777"
OTHER_MODERATE = "GHSA-8888-9999-cccc"

GOOD_CONFIG = {
    "schema_version": 1,
    "advisory": EXC,
    "approved": True,
    "approved_by": "test-fixture",
    "approval_reference": "https://github.com/AhmedMahmoud2222/DCIM/issues/364#issuecomment-1",
    "approved_on": "2026-11-18",
    "expires_utc": "2026-12-18T00:00:00Z",
}
GOOD_ADVISORY = {
    "ghsa_id": EXC,
    "withdrawn_at": None,
    "vulnerabilities": [
        {"package": {"name": "picomatch", "ecosystem": "npm"}, "vulnerable_version_range": "<= 4.0.7", "first_patched_version": None}
    ],
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def advisory(ghsa: str, package: str, severity: str, rng: str = "<=1.0.0") -> dict:
    return {
        "source": 1,
        "name": package,
        "dependency": package,
        "title": "t",
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": severity,
        "cwe": [],
        "cvss": {"score": 0, "vectorString": None},
        "range": rng,
    }


def node(name: str, severity: str, via: list) -> dict:
    return {"name": name, "severity": severity, "isDirect": False, "via": via, "effects": [], "range": "*", "nodes": [f"node_modules/{name}"], "fixAvailable": False}


def report(*nodes: dict, **overrides) -> str:
    counts = {s: 0 for s in gate.SEVERITIES}
    for n in nodes:
        counts[n["severity"]] += 1
    counts["total"] = len(nodes)
    doc = {
        "auditReportVersion": 2,
        "vulnerabilities": {n["name"]: n for n in nodes},
        "metadata": {"vulnerabilities": counts, "dependencies": {"total": 1}},
    }
    doc.update(overrides)
    return json.dumps(doc)


EXC_ADV = advisory(EXC, "picomatch", "high", "<=4.0.7")
EXC_ONLY = report(node("picomatch", "high", [EXC_ADV]), node("vite", "high", ["picomatch"]))
CLEAN = fixture("pr366_prod_audit.json")  # a real, valid report with no vulnerabilities


class FakeNpm:
    """Stands in for npm: records calls and returns canned results."""

    def __init__(self, dev=None, prod=None, versions=None, dev_code=1, prod_code=0, view_code=0):
        self.dev = fixture("pr366_dev_audit.json") if dev is None else dev
        self.prod = fixture("pr366_prod_audit.json") if prod is None else prod
        self.versions = fixture("picomatch_versions.json") if versions is None else versions
        self.dev_code, self.prod_code, self.view_code = dev_code, prod_code, view_code
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        if list(args[:3]) == ["npm", "view", "picomatch"]:
            return gate.CommandResult(self.view_code, self.versions)
        if "--omit=dev" in args:
            return gate.CommandResult(self.prod_code, self.prod)
        return gate.CommandResult(self.dev_code, self.dev)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.frontend = self.tmp / "frontend"
        self.write_tree({"src/App.tsx": "export const a = 1;\n", "e2e/smoke.spec.ts": "export {};\n"})
        self.config = self.tmp / "cfg.json"

    def write_tree(self, files: dict[str, str | bytes]) -> None:
        for rel, content in files.items():
            path = self.frontend / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")

    def write_config(self, **changes) -> None:
        doc = dict(GOOD_CONFIG)
        doc.update(changes)
        for key in [k for k, v in changes.items() if v is gate]:  # sentinel: delete the key
            del doc[key]
        self.config.write_text(json.dumps(doc), encoding="utf-8")

    def run_gate(self, mode="exception", npm=None, advisory_doc=GOOD_ADVISORY, now=NOW, config=True):
        if config is True:
            self.write_config()
        out: list[str] = []
        fetcher = advisory_doc if callable(advisory_doc) else (lambda: advisory_doc)
        code = gate.run_gate(mode, self.frontend, self.config, now, npm or FakeNpm(), fetcher, out.append)
        return code, "\n".join(out)

    def assertFailed(self, result, *needles):
        code, output = result
        self.assertEqual(code, 1, output)
        for needle in needles:
            self.assertIn(needle, output)

    def assertPassed(self, result):
        self.assertEqual(result[0], 0, result[1])


# ----------------------------------------------------------------------------- positive


class ControlledAcceptanceTests(Base):
    def test_only_the_approved_advisory_with_valid_future_authorization_passes(self):
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)
        self.assertIn("SECURITY EXCEPTION ACTIVE", output)
        self.assertIn(EXC, output)

    def test_real_main_baseline_with_tolerated_moderate_advisories_passes(self):
        npm = FakeNpm(dev=fixture("main_baseline_dev_audit.json"), prod=fixture("main_baseline_prod_audit.json"))
        self.assertPassed(self.run_gate(npm=npm))

    def test_synthetic_exception_only_report_passes(self):
        self.assertPassed(self.run_gate(npm=FakeNpm(dev=EXC_ONLY)))

    def test_high_node_with_exception_plus_moderate_root_is_tolerated(self):
        mod = advisory(OTHER_MODERATE, "vite", "moderate")
        dev = report(node("picomatch", "high", [EXC_ADV]), node("vite", "high", [mod, "picomatch"]))
        self.assertPassed(self.run_gate(npm=FakeNpm(dev=dev)))

    def test_valid_audit_with_no_vulnerabilities_passes_in_both_modes(self):
        for mode in ("strict", "exception"):
            with self.subTest(mode=mode):
                self.assertPassed(self.run_gate(mode, npm=FakeNpm(dev=CLEAN, dev_code=0)))

    def test_moderate_only_report_is_not_gating(self):
        dev = report(node("vite", "moderate", [advisory(OTHER_MODERATE, "vite", "moderate")]))
        self.assertPassed(self.run_gate("strict", npm=FakeNpm(dev=dev, dev_code=0)))


# ----------------------------------------------------------------------------- approval and expiry


class ConfigurationTests(Base):
    def test_missing_configuration_file_fails(self):
        self.assertFailed(self.run_gate(config=False), "absent or unreadable")

    def test_shipped_configuration_is_unapproved_and_fails(self):
        self.config = SHIPPED_CONFIG
        self.assertFailed(self.run_gate(config=False), "NOT APPROVED")

    def test_invalid_configurations_fail(self):
        cases = {
            "approved false": ({"approved": False}, "NOT APPROVED"),
            "approved string": ({"approved": "true"}, "NOT APPROVED"),
            "approved one": ({"approved": 1}, "NOT APPROVED"),
            "wrong advisory": ({"advisory": "GHSA-2222-3333-4444"}, "not " + EXC),
            "package wide": ({"advisory": "picomatch"}, "not " + EXC),
            "bad schema": ({"schema_version": 2}, "schema_version"),
            "bool schema": ({"schema_version": True}, "schema_version"),
            "empty approver": ({"approved_by": " "}, "approved_by"),
            "foreign reference": ({"approval_reference": "https://example.com/ok"}, "approval_reference"),
            "other repo reference": ({"approval_reference": "https://github.com/x/y/issues/1#issuecomment-1"}, "approval_reference"),
            "bad approved_on": ({"approved_on": "18/11/2026"}, "approved_on"),
            "impossible date": ({"approved_on": "2026-02-31"}, "calendar"),
            "future approval": ({"approved_on": "2026-11-20"}, "future"),
            "null expiry": ({"expires_utc": None}, "expires_utc"),
            "empty expiry": ({"expires_utc": ""}, "expires_utc"),
            "naive expiry": ({"expires_utc": "2026-12-18T00:00:00"}, "expires_utc"),
            "offset expiry": ({"expires_utc": "2026-12-18T00:00:00+02:00"}, "expires_utc"),
            "date-only expiry": ({"expires_utc": "2026-12-18"}, "expires_utc"),
            "expiry before approval": ({"expires_utc": "2026-11-17T00:00:00Z"}, "after approved_on"),
            "lifetime too long": ({"expires_utc": "2027-01-18T00:00:00Z"}, "longer than"),
            "already expired": ({"expires_utc": "2026-11-19T11:59:59Z"}, "expired"),
        }
        for label, (changes, needle) in cases.items():
            with self.subTest(label):
                self.write_config(**changes)
                self.assertFailed(self.run_gate(config=False), needle)

    def test_missing_and_extra_keys_fail(self):
        for key in gate.CONFIG_KEYS:
            with self.subTest(missing=key):
                self.write_config(**{key: gate})
                self.assertFailed(self.run_gate(config=False), "exactly the keys")
        self.write_config(note="extra")
        self.assertFailed(self.run_gate(config=False), "exactly the keys")

    def test_corrupt_configuration_fails(self):
        for text in ("", "{", "[]", "null", '"approved"'):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                self.assertFailed(self.run_gate(config=False))

    def test_expiry_boundaries_in_utc(self):
        expires = dt.datetime(2026, 12, 18, 0, 0, 0, tzinfo=UTC)
        self.write_config()
        one = dt.timedelta(seconds=1)
        self.assertPassed(self.run_gate(now=expires - one, config=False))
        self.assertFailed(self.run_gate(now=expires, config=False), "expired")
        self.assertFailed(self.run_gate(now=expires + one, config=False), "expired")
        # The same instant expressed in another zone is still the same instant.
        local = (expires - one).astimezone(dt.timezone(dt.timedelta(hours=-8)))
        with self.assertRaises(gate.GateError):
            gate.load_config(self.config, local)  # a non-UTC clock is rejected, never guessed

    def test_expired_exception_fails_even_when_the_audit_is_clean(self):
        self.write_config(expires_utc="2026-11-19T11:00:00Z")
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=CLEAN, dev_code=0), config=False), "expired")

    def test_approval_in_the_future_boundary(self):
        self.write_config(approved_on="2026-11-19")
        self.assertPassed(self.run_gate(now=dt.datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC), config=False))
        self.assertFailed(self.run_gate(now=dt.datetime(2026, 11, 18, 23, 59, 59, tzinfo=UTC), config=False), "future")


# ----------------------------------------------------------------------------- advisory scope


class AdvisoryScopeTests(Base):
    def test_another_high_advisory_fails(self):
        dev = report(node("picomatch", "high", [EXC_ADV]), node("evil", "high", [advisory(OTHER_HIGH, "evil", "high")]))
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "not exclusively caused", "evil")

    def test_another_critical_advisory_fails(self):
        dev = report(node("picomatch", "high", [EXC_ADV]), node("evil", "critical", [advisory(OTHER_CRITICAL, "evil", "critical")]))
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "evil")

    def test_unrelated_high_advisory_inside_a_mixed_transitive_chain_fails(self):
        dev = report(
            node("picomatch", "high", [EXC_ADV]),
            node("vite", "high", ["picomatch", "evil"]),
            node("evil", "high", [advisory(OTHER_HIGH, "evil", "high")]),
        )
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "vite", "evil")

    def test_unrelated_high_advisory_alone_fails_in_strict_mode(self):
        dev = report(node("evil", "high", [advisory(OTHER_HIGH, "evil", "high")]))
        self.assertFailed(self.run_gate("strict", npm=FakeNpm(dev=dev)), "evil")

    def test_package_name_is_not_an_allowlist(self):
        dev = report(node("picomatch", "high", [EXC_ADV, advisory(OTHER_HIGH, "picomatch", "high", "<=9.9.9")]))
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "picomatch")

    def test_exception_advisory_metadata_change_fails(self):
        for rng in ("<=4.0.9", "<5.0.0", "*"):
            with self.subTest(rng=rng):
                dev = report(node("picomatch", "high", [advisory(EXC, "picomatch", "high", rng)]))
                self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "metadata changed")
        dev = report(node("picomatch", "high", [advisory(EXC, "picomatch", "critical", "<=4.0.7")]))
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "metadata changed")
        dev = report(node("other", "high", [advisory(EXC, "other", "high", "<=4.0.7")]))
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=dev)), "metadata changed")

    def test_chain_cycles_terminate_and_resolve(self):
        dev = report(
            node("picomatch", "high", [EXC_ADV]),
            node("a", "high", ["b", "picomatch"]),
            node("b", "high", ["a"]),
        )
        self.assertPassed(self.run_gate(npm=FakeNpm(dev=dev)))


# ----------------------------------------------------------------------------- production


class ProductionGateTests(Base):
    PROD_VULN = report(node("express", "low", [advisory(OTHER_MODERATE, "express", "low")]))

    def test_a_production_vulnerability_fails_in_both_modes(self):
        for mode in ("strict", "exception"):
            with self.subTest(mode=mode):
                npm = FakeNpm(prod=self.PROD_VULN, prod_code=1)
                self.assertFailed(self.run_gate(mode, npm=npm), "production dependencies report 1")

    def test_a_production_vulnerability_is_not_masked_by_a_zero_exit(self):
        self.assertFailed(self.run_gate(npm=FakeNpm(prod=self.PROD_VULN, prod_code=0)), "production dependencies report 1")

    def test_production_gate_uses_the_documented_command(self):
        npm = FakeNpm()
        self.run_gate(npm=npm)
        self.assertIn(["npm", "audit", "--omit=dev", "--audit-level=low", "--json"], npm.calls)
        self.assertIn(["npm", "audit", "--audit-level=high", "--json"], npm.calls)

    def test_empty_production_report_with_nonzero_exit_fails(self):
        self.assertFailed(self.run_gate(npm=FakeNpm(prod_code=1)), "exited nonzero")


# ----------------------------------------------------------------------------- input validation


class InputValidationTests(Base):
    def assertDevRejected(self, text, needle=None, code=1):
        result = self.run_gate(npm=FakeNpm(dev=text, dev_code=code))
        self.assertFailed(result, *( [needle] if needle else [] ))

    def test_malformed_and_truncated_json_fail(self):
        good = fixture("pr366_dev_audit.json")
        for label, text in {
            "empty": "",
            "whitespace": "  \n",
            "garbage": "not json",
            "truncated": good[: len(good) // 2],
            "truncated-tail": good[:-5],
            "array": "[]",
            "null": "null",
        }.items():
            with self.subTest(label):
                self.assertDevRejected(text)

    def test_npm_error_payload_fails(self):
        err = json.dumps({"error": {"code": "ENOTFOUND", "summary": "request to registry failed", "detail": "x"}})
        self.assertDevRejected(err, "reported an error", code=1)
        self.assertDevRejected(err, "reported an error", code=0)

    def test_missing_required_keys_fail(self):
        doc = json.loads(fixture("pr366_dev_audit.json"))
        variants = {
            "no metadata": lambda d: d.pop("metadata"),
            "no vulnerabilities": lambda d: d.pop("vulnerabilities"),
            "no metadata.vulnerabilities": lambda d: d["metadata"].pop("vulnerabilities"),
            "no total": lambda d: d["metadata"]["vulnerabilities"].pop("total"),
            "no high count": lambda d: d["metadata"]["vulnerabilities"].pop("high"),
            "no report version": lambda d: d.pop("auditReportVersion"),
            "vulnerabilities is list": lambda d: d.__setitem__("vulnerabilities", []),
            "count is string": lambda d: d["metadata"]["vulnerabilities"].__setitem__("high", "10"),
            "count is bool": lambda d: d["metadata"]["vulnerabilities"].__setitem__("high", True),
            "negative count": lambda d: d["metadata"]["vulnerabilities"].__setitem__("low", -1),
        }
        for label, mutate in variants.items():
            with self.subTest(label):
                copy = json.loads(json.dumps(doc))
                mutate(copy)
                self.assertDevRejected(json.dumps(copy))

    def test_schema_variations_fail(self):
        doc = json.loads(fixture("pr366_dev_audit.json"))
        variants = {
            "report version 1": lambda d: d.__setitem__("auditReportVersion", 1),
            "report version 3": lambda d: d.__setitem__("auditReportVersion", 3),
            "report version string": lambda d: d.__setitem__("auditReportVersion", "2"),
            "unknown severity": lambda d: d["vulnerabilities"]["picomatch"].__setitem__("severity", "urgent"),
            "severity null": lambda d: d["vulnerabilities"]["picomatch"].__setitem__("severity", None),
            "name mismatch": lambda d: d["vulnerabilities"]["fdir"].__setitem__("name", "other"),
            "empty via": lambda d: d["vulnerabilities"]["fdir"].__setitem__("via", []),
            "via not list": lambda d: d["vulnerabilities"]["fdir"].__setitem__("via", "picomatch"),
            "via element number": lambda d: d["vulnerabilities"]["fdir"].__setitem__("via", [1]),
            "unknown dependency": lambda d: d["vulnerabilities"]["fdir"].__setitem__("via", ["ghost"]),
            "non-GHSA url": lambda d: d["vulnerabilities"]["picomatch"]["via"][0].__setitem__("url", "https://npmjs.com/advisories/1"),
            "no url": lambda d: d["vulnerabilities"]["picomatch"]["via"][0].pop("url"),
            "no advisory range": lambda d: d["vulnerabilities"]["picomatch"]["via"][0].pop("range"),
            "advisory severity unknown": lambda d: d["vulnerabilities"]["picomatch"]["via"][0].__setitem__("severity", "x"),
            "count mismatch": lambda d: d["metadata"]["vulnerabilities"].update(high=9, moderate=1),
            "total mismatch": lambda d: d["metadata"]["vulnerabilities"].__setitem__("total", 11),
            "sum mismatch": lambda d: d["metadata"]["vulnerabilities"].update(total=9, high=9),
        }
        for label, mutate in variants.items():
            with self.subTest(label):
                copy = json.loads(json.dumps(doc))
                mutate(copy)
                self.assertDevRejected(json.dumps(copy))

    def test_unresolvable_roots_and_inconsistent_advisories_fail(self):
        cyc = report(node("a", "high", ["b"]), node("b", "high", ["a"]))
        self.assertDevRejected(cyc, "root advisory")
        inflated = report(node("x", "high", [advisory(OTHER_MODERATE, "x", "moderate")]))
        self.assertDevRejected(inflated, "higher than any")
        conflict = report(
            node("p", "high", [advisory(OTHER_HIGH, "p", "high")]),
            node("q", "moderate", [advisory(OTHER_HIGH, "p", "moderate")]),
        )
        self.assertDevRejected(conflict, "inconsistent metadata")

    def test_production_report_is_validated_with_the_same_strictness(self):
        for text in ("", "{", "[]", json.dumps({"error": {"code": "E"}}), CLEAN.replace('"auditReportVersion": 2', '"auditReportVersion": 9')):
            with self.subTest(text=text[:30]):
                self.assertFailed(self.run_gate(npm=FakeNpm(prod=text)))


class NpmExitStatusTests(Base):
    def test_unexpected_exit_statuses_fail(self):
        for code in (2, 3, 126, 127, 130, 137, -9, -11, 255):
            with self.subTest(dev=code):
                self.assertFailed(self.run_gate(npm=FakeNpm(dev_code=code)), "unexpected status")
            with self.subTest(prod=code):
                self.assertFailed(self.run_gate(npm=FakeNpm(prod_code=code)), "unexpected status")

    def test_exit_zero_with_high_findings_fails(self):
        self.assertFailed(self.run_gate(npm=FakeNpm(dev_code=0)), "exited 0")

    def test_exit_one_without_findings_fails(self):
        self.assertFailed(self.run_gate(npm=FakeNpm(dev=CLEAN, dev_code=1)), "exited 1")

    def test_runner_failures_are_failures(self):
        def timeout(args, cwd):
            raise gate.GateError("npm audit timed out")

        out: list[str] = []
        self.write_config()
        code = gate.run_gate("exception", self.frontend, self.config, NOW, timeout, lambda: GOOD_ADVISORY, out.append)
        self.assertEqual(code, 1)

    def test_a_missing_binary_is_a_failure(self):
        with self.assertRaises(gate.GateError):
            gate.run_command(["definitely-not-a-real-binary-xyz"], self.tmp)


# ----------------------------------------------------------------------------- upstream detection


class UpstreamDetectionTests(Base):
    def versions(self, *extra, drop=()):
        base = json.loads(fixture("picomatch_versions.json"))
        return json.dumps([v for v in base if v not in drop] + list(extra))

    def test_fixture_registry_state_passes(self):
        self.assertPassed(self.run_gate())

    def test_newer_picomatch_release_fails(self):
        for version in ("4.0.8", "4.1.0", "5.0.0", "4.0.8-rc.1", "4.1.0-beta.0", "10.0.0"):
            with self.subTest(version):
                self.assertFailed(self.run_gate(npm=FakeNpm(versions=self.versions(version))), "newer than 4.0.7")

    def test_older_line_backport_numbers_do_not_trigger_but_prereleases_of_4_0_7_do_not_either(self):
        self.assertPassed(self.run_gate(npm=FakeNpm(versions=self.versions("2.3.2", "3.0.2", "4.0.7-rc.1"))))

    def test_registry_failures_fail(self):
        cases = {
            "npm view fails": FakeNpm(view_code=1),
            "npm view e404": FakeNpm(view_code=1, versions=json.dumps({"error": {"code": "E404"}})),
            "empty": FakeNpm(versions=""),
            "invalid json": FakeNpm(versions="{"),
            "empty list": FakeNpm(versions="[]"),
            "object": FakeNpm(versions="{}"),
            "invalid version": FakeNpm(versions=self.versions("latest")),
            "number version": FakeNpm(versions=json.dumps([4, 0, 7])),
            "missing last vulnerable version": FakeNpm(versions=self.versions(drop=("4.0.7",))),
        }
        for label, npm in cases.items():
            with self.subTest(label):
                self.assertFailed(self.run_gate(npm=npm))

    def test_advisory_api_outage_and_bad_data_fail(self):
        def outage():
            raise gate.GateError("advisory API unreachable: URLError")

        bad = {
            "outage": outage,
            "not a dict": [],
            "wrong advisory": {**GOOD_ADVISORY, "ghsa_id": "GHSA-2222-3333-4444"},
            "withdrawn": {**GOOD_ADVISORY, "withdrawn_at": "2026-12-01T00:00:00Z"},
            "no vulnerabilities": {**GOOD_ADVISORY, "vulnerabilities": []},
            "vulnerabilities not list": {**GOOD_ADVISORY, "vulnerabilities": {}},
            "entry not dict": {**GOOD_ADVISORY, "vulnerabilities": ["x"]},
        }
        for label, doc in bad.items():
            with self.subTest(label):
                self.assertFailed(self.run_gate(advisory_doc=doc))

    def test_patched_version_or_changed_scope_in_the_advisory_fails(self):
        def with_entry(**changes):
            entry = {**GOOD_ADVISORY["vulnerabilities"][0], **changes}
            return {**GOOD_ADVISORY, "vulnerabilities": [entry]}

        cases = {
            "patched string": with_entry(first_patched_version="4.0.8"),
            "patched object": with_entry(first_patched_version={"identifier": "4.0.8"}),
            "range changed": with_entry(vulnerable_version_range="<= 4.0.9"),
            "range missing": with_entry(vulnerable_version_range=None),
            "other package": with_entry(package={"name": "minimatch", "ecosystem": "npm"}),
            "other ecosystem": with_entry(package={"name": "picomatch", "ecosystem": "pip"}),
            "extra package": {**GOOD_ADVISORY, "vulnerabilities": [*GOOD_ADVISORY["vulnerabilities"], {"package": {"name": "x", "ecosystem": "npm"}, "vulnerable_version_range": "<= 1", "first_patched_version": None}]},
        }
        for label, doc in cases.items():
            with self.subTest(label):
                self.assertFailed(self.run_gate(advisory_doc=doc))

    def test_advisory_range_spacing_is_normalised(self):
        for rng in ("<= 4.0.7", "<=4.0.7", "<=  4.0.7"):
            with self.subTest(rng):
                entry = {**GOOD_ADVISORY["vulnerabilities"][0], "vulnerable_version_range": rng}
                self.assertPassed(self.run_gate(advisory_doc={**GOOD_ADVISORY, "vulnerabilities": [entry]}))


class AdvisoryHttpTests(unittest.TestCase):
    def response(self, status=200, body=b"{}"):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_success_returns_parsed_json(self):
        with mock.patch.object(gate.urllib.request, "urlopen", return_value=self.response(body=b'{"ghsa_id": "x"}')):
            self.assertEqual(gate.fetch_advisory_http(), {"ghsa_id": "x"})

    def test_every_transport_failure_is_a_gate_error(self):
        failures = [
            urllib.error.URLError("no network"),
            urllib.error.HTTPError(gate.ADVISORY_API_URL, 500, "boom", {}, io.BytesIO(b"")),
            urllib.error.HTTPError(gate.ADVISORY_API_URL, 403, "rate limited", {}, io.BytesIO(b"")),
            TimeoutError(),
            ConnectionResetError(),
            OSError("x"),
        ]
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                with mock.patch.object(gate.urllib.request, "urlopen", side_effect=failure):
                    with self.assertRaises(gate.GateError):
                        gate.fetch_advisory_http()

    def test_bad_status_and_bad_bodies_are_gate_errors(self):
        for resp in (self.response(status=204), self.response(body=b"not json"), self.response(body=b"\xff\xfe")):
            with self.subTest(status=resp.status, body=resp.read.return_value):
                with mock.patch.object(gate.urllib.request, "urlopen", return_value=resp):
                    with self.assertRaises(gate.GateError):
                        gate.fetch_advisory_http()


# ----------------------------------------------------------------------------- source guard


class SourceGuardTests(Base):
    def test_clean_tree_passes(self):
        self.assertPassed(self.run_gate())

    def test_real_repository_sources_pass(self):
        self.assertEqual(gate.scan_sources(ROOT / "frontend"), [])

    def test_prohibited_module_references_fail(self):
        snippets = {
            "import default": 'import pm from "picomatch";',
            "import named": "import { minimatch } from 'minimatch';",
            "subpath": 'import x from "picomatch/lib/parse";',
            "require": 'const m = require("minimatch");',
            "dynamic import": 'const t = await import("tinyglobby");',
            "fdir": 'import { fdir } from "fdir";',
            "template literal": "const t = await import(`fdir`);",
            "vi.mock": 'vi.mock("picomatch", () => ({}));',
            "reexport": 'export * from "tinyglobby";',
            "side effect": 'import "minimatch";',
        }
        for label, code in snippets.items():
            for directory in ("src", "e2e"):
                with self.subTest(label=label, directory=directory):
                    self.write_tree({f"{directory}/x.ts": code + "\n"})
                    self.assertFailed(self.run_gate(), "prohibited")
                    (self.frontend / directory / "x.ts").unlink()

    def test_non_literal_or_unsupported_import_meta_glob_fails(self):
        snippets = {
            "variable": "const f = import.meta.glob(pattern);",
            "template with substitution": "const f = import.meta.glob(`./${dir}/*.ts`);",
            "concatenation": 'const f = import.meta.glob("./" + dir);',
            "array with variable": 'const f = import.meta.glob(["./a/*.ts", extra]);',
            "eager variant": 'const f = import.meta.globEager("./a/*.ts");',
            "member": "const g = import.meta.glob;",
            "optional call": "const g = import.meta.glob?.(p);",
        }
        for label, code in snippets.items():
            with self.subTest(label):
                self.write_tree({"src/g.ts": code + "\n"})
                self.assertFailed(self.run_gate(), "import.meta.glob")

    def test_literal_import_meta_glob_is_allowed(self):
        snippets = (
            'const f = import.meta.glob("./a/*.ts");',
            "const f = import.meta.glob('./a/*.ts', { eager: true });",
            "const f = import.meta.glob<string>([\"../**/*.{ts,tsx,css}\", \"!../**/*.test.{ts,tsx}\"], { query: \"?raw\" });",
            "const f = import.meta.glob(\n  [\n    \"./a/*.ts\",\n    './b/*.ts',\n  ],\n  { eager: true },\n);",
            "const f = import.meta.glob(`./a/*.ts`);",
        )
        for code in snippets:
            with self.subTest(code=code[:40]):
                self.write_tree({"src/g.ts": code + "\n"})
                self.assertPassed(self.run_gate())

    def test_unverifiable_trees_fail_closed(self):
        import shutil

        shutil.rmtree(self.frontend / "e2e")
        self.assertFailed(self.run_gate(), "missing")
        self.write_tree({"e2e/ok.ts": "export {};\n"})
        shutil.rmtree(self.frontend / "src")
        self.assertFailed(self.run_gate(), "missing")

    def test_undecodable_text_file_fails_but_binary_assets_are_skipped(self):
        self.write_tree({"src/logo.png": b"\xff\xfe\x00binary"})
        self.assertPassed(self.run_gate())
        self.write_tree({"src/data.ts": b"\xff\xfe\x00broken"})
        self.assertFailed(self.run_gate(), "unreadable")

    def test_symlinks_fail_closed(self):
        target = self.tmp / "outside.ts"
        target.write_text('import "picomatch";\n', encoding="utf-8")
        link = self.frontend / "src" / "link.ts"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertFailed(self.run_gate(), "symlink")

    def test_source_guard_belongs_to_exception_mode_and_strict_mode_matches_the_existing_gate(self):
        self.write_tree({"src/x.ts": 'import "picomatch";\n'})
        self.assertPassed(self.run_gate("strict", npm=FakeNpm(dev=CLEAN, dev_code=0)))
        self.assertFailed(self.run_gate("exception", npm=FakeNpm(dev=CLEAN, dev_code=0)), "prohibited")


# ----------------------------------------------------------------------------- strict mode


class StrictModeTests(Base):
    def test_strict_mode_never_grants_the_exception(self):
        self.write_config()  # even a fully valid approval is ignored in strict mode
        for dev in (fixture("pr366_dev_audit.json"), fixture("main_baseline_dev_audit.json"), EXC_ONLY):
            with self.subTest(dev=dev[:40]):
                result = self.run_gate("strict", npm=FakeNpm(dev=dev), config=False)
                self.assertFailed(result, "no exception is active in strict mode")

    def test_strict_mode_does_not_read_configuration_or_network(self):
        called = []
        npm = FakeNpm(dev=CLEAN, dev_code=0)
        result = self.run_gate("strict", npm=npm, advisory_doc=lambda: called.append(1), config=False)
        self.assertPassed(result)
        self.assertEqual(called, [])
        self.assertFalse(any(c[:2] == ["npm", "view"] for c in npm.calls))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.run_gate("lenient", self.frontend, self.config, NOW, FakeNpm(), lambda: GOOD_ADVISORY, lambda s: None)


# ----------------------------------------------------------------------------- fail-closed entry point


class EntryPointTests(Base):
    def main(self, **kwargs):
        args = ["--mode", kwargs.pop("mode", "exception"), "--frontend-dir", str(self.frontend), "--exception-config", str(self.config)]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = gate.main(args, **kwargs)
        return code, stream.getvalue()

    def test_valid_run_passes_through_main(self):
        self.write_config()
        code, out = self.main(runner=FakeNpm(), advisory_fetcher=lambda: GOOD_ADVISORY, now=NOW)
        self.assertEqual(code, 0, out)

    def test_unexpected_errors_cannot_become_success(self):
        self.write_config()
        boom = {
            "runner RuntimeError": dict(runner=mock.Mock(side_effect=RuntimeError("boom")), advisory_fetcher=lambda: GOOD_ADVISORY),
            "runner KeyError": dict(runner=mock.Mock(side_effect=KeyError("x")), advisory_fetcher=lambda: GOOD_ADVISORY),
            "runner OSError": dict(runner=mock.Mock(side_effect=OSError("x")), advisory_fetcher=lambda: GOOD_ADVISORY),
            "runner returns None": dict(runner=lambda a, c: None, advisory_fetcher=lambda: GOOD_ADVISORY),
            "runner returns str": dict(runner=lambda a, c: "ok", advisory_fetcher=lambda: GOOD_ADVISORY),
            "fetcher TypeError": dict(runner=FakeNpm(), advisory_fetcher=mock.Mock(side_effect=TypeError("x"))),
            "fetcher ZeroDivision": dict(runner=FakeNpm(), advisory_fetcher=mock.Mock(side_effect=ZeroDivisionError())),
        }
        for label, kwargs in boom.items():
            with self.subTest(label):
                code, out = self.main(now=NOW, **kwargs)
                self.assertEqual(code, 1, out)
        code, out = self.main(now=NOW, **boom["runner RuntimeError"])
        self.assertIn("failing closed", out)

    def test_run_gate_internal_error_returns_failure_not_success(self):
        self.write_config()
        with mock.patch.object(gate, "run_gate", side_effect=ValueError("bug")):
            code, out = self.main(runner=FakeNpm(), advisory_fetcher=lambda: GOOD_ADVISORY, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("failing closed", out)

    def test_gate_errors_are_reported_as_failures(self):
        with mock.patch.object(gate, "run_gate", side_effect=gate.GateError("nope")):
            code, out = self.main(runner=FakeNpm(), now=NOW)
        self.assertEqual((code, "nope" in out), (1, True))

    def test_default_mode_is_strict(self):
        self.write_config()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = gate.main(["--frontend-dir", str(self.frontend), "--exception-config", str(self.config)], runner=FakeNpm(), advisory_fetcher=lambda: GOOD_ADVISORY, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("strict mode", stream.getvalue())

    def test_non_utc_clock_is_never_accepted(self):
        self.write_config()
        local = NOW.astimezone(dt.timezone(dt.timedelta(hours=3)))
        code, out = self.main(runner=FakeNpm(), advisory_fetcher=lambda: GOOD_ADVISORY, now=local)
        self.assertEqual(code, 1, out)
        naive = dt.datetime(2026, 11, 19, 12, 0, 0)
        code, out = self.main(runner=FakeNpm(), advisory_fetcher=lambda: GOOD_ADVISORY, now=naive)
        self.assertEqual(code, 1, out)


# ----------------------------------------------------------------------------- activation boundary


class ActivationBoundaryTests(unittest.TestCase):
    """These tests are the tripwire for activation. Changing them is the deliberate signal that a
    separately authorized activation was reviewed (see .github/security-exceptions/README.md)."""

    def test_the_exception_is_not_approved_in_this_change(self):
        doc = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
        self.assertIs(doc["approved"], False, "SECURITY EXCEPTION NOT APPROVED: the shipped record must stay unapproved")
        self.assertEqual(set(doc), set(gate.CONFIG_KEYS))
        for key in ("approved_by", "approval_reference", "approved_on", "expires_utc"):
            self.assertIsNone(doc[key], f"{key} must stay empty until the owner authorizes the exception")

    def test_the_shipped_record_cannot_activate_the_exception(self):
        with self.assertRaises(gate.GateError):
            gate.load_config(SHIPPED_CONFIG, NOW)

    def test_the_default_configuration_path_is_the_shipped_record(self):
        self.assertEqual((ROOT / gate.DEFAULT_CONFIG_RELATIVE).resolve(), SHIPPED_CONFIG.resolve())

    def test_no_workflow_calls_the_wrapper(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
            with self.subTest(path=path.name):
                self.assertNotIn("frontend_audit_gate", path.read_text(encoding="utf-8"))

    def test_the_enforced_audit_step_is_unchanged(self):
        workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["frontend-suite"]["steps"]
        audit = [s for s in steps if s.get("name") == "Audit for high/critical vulnerabilities (fails build)"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["run"].strip(), "npm audit --audit-level=high")
        for key in ("continue-on-error", "if", "env"):
            self.assertNotIn(key, audit[0])
        self.assertNotIn("continue-on-error", workflow["jobs"]["frontend-suite"])

    def test_exception_constants_are_pinned(self):
        self.assertEqual(gate.EXCEPTION_ADVISORY, "GHSA-fc5q-39xc-vjpf")
        self.assertEqual(gate.EXCEPTION_PACKAGE, "picomatch")
        self.assertEqual(gate.MAX_LIFETIME_DAYS, 45)


class FixtureIntegrityTests(unittest.TestCase):
    def test_real_fixtures_parse_and_match_the_documented_state(self):
        pr = gate.parse_audit(fixture("pr366_dev_audit.json"))
        self.assertEqual((pr.counts["high"], pr.counts["moderate"], pr.counts["critical"]), (10, 0, 0))
        self.assertTrue(gate.analyze(pr).exception_only)
        base = gate.parse_audit(fixture("main_baseline_dev_audit.json"))
        self.assertEqual((base.counts["high"], base.counts["moderate"]), (6, 5))
        self.assertTrue(gate.analyze(base).exception_only)
        for name in ("pr366_prod_audit.json", "main_baseline_prod_audit.json"):
            self.assertEqual(gate.parse_audit(fixture(name)).total, 0)
        self.assertEqual(max(gate.parse_version(v)[:3] for v in json.loads(fixture("picomatch_versions.json"))), gate.LAST_VULNERABLE)


if __name__ == "__main__":
    unittest.main()
