"""Approval, expiry, advisory scope, production and strict-mode tests for the audit gate.

SECURITY EXCEPTION NOT APPROVED OR ACTIVE. See frontend_audit_support.py for the shared fixtures.
"""

from __future__ import annotations

import datetime as dt
import unittest


from frontend_audit_support import (
    Base,
    CLEAN,
    EXC,
    EXC_ADV,
    EXC_ONLY,
    FakeNpm,
    GOOD_ADVISORY,
    NOW,
    OTHER_CRITICAL,
    OTHER_HIGH,
    OTHER_MODERATE,
    SHIPPED_CONFIG,
    UTC,
    advisory,
    fixture,
    gate,
    node,
    report,
)


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


if __name__ == "__main__":
    unittest.main()
