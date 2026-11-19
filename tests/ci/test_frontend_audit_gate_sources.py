"""Source-import guard, fail-closed entry point and activation-boundary tests for the audit gate.

SECURITY EXCEPTION NOT APPROVED OR ACTIVE. See frontend_audit_support.py for the shared fixtures.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import yaml

from ci_support import CI_YML

from frontend_audit_support import (
    Base,
    CLEAN,
    FakeNpm,
    GOOD_ADVISORY,
    NOW,
    ROOT,
    SHIPPED_CONFIG,
    fixture,
    gate,
)


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


if __name__ == "__main__":
    unittest.main()
