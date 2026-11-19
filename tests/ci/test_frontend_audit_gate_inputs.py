"""Input-validation, npm exit-status, upstream-detection and advisory-API tests for the audit gate.

SECURITY EXCEPTION NOT APPROVED OR ACTIVE. See frontend_audit_support.py for the shared fixtures.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from frontend_audit_support import (
    Base,
    CLEAN,
    FakeNpm,
    GOOD_ADVISORY,
    NOW,
    OTHER_HIGH,
    OTHER_MODERATE,
    advisory,
    fixture,
    gate,
    node,
    report,
)


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

    def test_the_real_runner_refuses_commands_outside_the_allowlist(self):
        for command in (["definitely-not-a-real-binary-xyz"], ["npm", "install"], ["npm", "audit", "--json"], ["sh", "-c", "true"], []):
            with self.subTest(command=command):
                with self.assertRaises(gate.GateError):
                    gate.run_command(command, self.tmp)

    def test_every_command_the_gate_issues_is_on_the_allowlist(self):
        npm = FakeNpm()
        self.run_gate(npm=npm)
        allowed = {gate.AUDIT_PROD_COMMAND, gate.AUDIT_DEV_COMMAND, gate.VERSIONS_COMMAND}
        self.assertTrue(npm.calls)
        for call in npm.calls:
            self.assertIn(tuple(call), allowed)


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
            if isinstance(failure, urllib.error.HTTPError):
                self.addCleanup(failure.close)
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


if __name__ == "__main__":
    unittest.main()
