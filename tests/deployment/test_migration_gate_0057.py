import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('gate', ROOT / 'backend/scripts/migration_gate_0057.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

OK_ENV = {gate.APPROVAL_VAR: gate.APPROVAL_VALUE, gate.CHANGE_ID_VAR: 'CHG-1'}
PENDING = ['0057_fk_indexes', '0056_pdu_vertical_placement']


class GateTests(unittest.TestCase):
    def go(self, env, pending=PENDING, current=lambda: ['0055_x']):
        calls = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gate.run(env, current, lambda c: pending(c) if callable(pending) else pending,
                          lambda: calls.append('upgrade') or 0)
        return rc, calls, err.getvalue()

    def test_blocked_without_approval(self):
        rc, calls, err = self.go({})
        self.assertEqual((rc, calls), (2, []))
        self.assertIn('BLOCKED', err)

    def test_partial_or_wrong_approval_blocked(self):
        for env in ({gate.APPROVAL_VAR: gate.APPROVAL_VALUE},
                    {gate.APPROVAL_VAR: gate.APPROVAL_VALUE, gate.CHANGE_ID_VAR: '  '},
                    {gate.APPROVAL_VAR: '1', gate.CHANGE_ID_VAR: 'CHG-1'},
                    {gate.APPROVAL_VAR: 'true', gate.CHANGE_ID_VAR: 'CHG-1'}):
            self.assertEqual(self.go(env)[:2], (2, []), env)

    def test_approved_runs_full_upgrade(self):
        self.assertEqual(self.go(OK_ENV)[:2], (0, ['upgrade']))

    def test_not_pending_is_unaffected(self):
        self.assertEqual(self.go({}, pending=['0058_next'])[:2], (0, ['upgrade']))
        self.assertEqual(self.go({}, pending=[])[:2], (0, ['upgrade']))

    def test_fail_closed_on_uncertainty(self):
        def boom(_):
            raise gate.GateError('db unreachable')
        rc, calls, _ = self.go(OK_ENV, pending=boom)
        self.assertEqual((rc, calls), (3, []))

    def test_upgrade_failure_propagates(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.run(OK_ENV, lambda: [], lambda c: PENDING, lambda: 1), 1)

    def test_real_pending_computation(self):
        # Reads the real revision files; needs no alembic installation.
        self.assertEqual(gate.pending_revisions(['0056_pdu_vertical_placement']), [gate.GATED_REVISION])
        self.assertEqual(gate.pending_revisions([gate.GATED_REVISION]), [])
        everything = gate.pending_revisions([])
        self.assertIn(gate.GATED_REVISION, everything)
        self.assertGreater(len(everything), 10)

    def test_unknown_or_multiple_current_fails_closed(self):
        with self.assertRaises(gate.GateError):
            gate.pending_revisions(['9999_unknown'])
        with self.assertRaises(gate.GateError):
            gate.pending_revisions(['0055_alarm_rule_resolution', '0056_pdu_vertical_placement'])

    def test_multiple_heads_and_unparseable_fail_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            v = Path(d)
            (v / 'a.py').write_text('revision = "a"\ndown_revision = None\n')
            (v / 'b.py').write_text('revision = "b"\ndown_revision = "a"\n')
            self.assertEqual(gate.pending_revisions(['a'], v), ['b'])
            (v / 'c.py').write_text('revision = "c"\ndown_revision = "a"\n')
            with self.assertRaises(gate.GateError):
                gate.pending_revisions([], v)
            (v / 'c.py').write_text('x = 1\n')
            with self.assertRaises(gate.GateError):
                gate.pending_revisions([], v)

    def test_end_to_end_blocks_real_graph_without_approval(self):
        calls = []
        with contextlib.redirect_stderr(io.StringIO()):
            rc = gate.run({}, lambda: ['0056_pdu_vertical_placement'], gate.pending_revisions,
                          lambda: calls.append('upgrade') or 0)
        self.assertEqual((rc, calls), (2, []))


class ComposeWiringTests(unittest.TestCase):
    def test_production_migrate_uses_gate_not_raw_alembic(self):
        text = (ROOT / 'docker-compose.production.yml').read_text()
        block = text[text.index('  migrate:'):text.index('  bootstrap-privileges:')]
        self.assertIn('scripts/migration_gate_0057.py', block)
        self.assertNotIn('"upgrade"', block)
        self.assertIn('DCIM_APPROVE_MIGRATION_0057: ${DCIM_APPROVE_MIGRATION_0057:-}', block)
        self.assertIn('DCIM_MIGRATION_0057_CHANGE_ID: ${DCIM_MIGRATION_0057_CHANGE_ID:-}', block)

    def test_dev_ci_compose_and_deploy_protections_unchanged(self):
        self.assertNotIn('migration_gate_0057', (ROOT / 'docker-compose.yml').read_text())
        script = (ROOT / 'scripts/deploy-docker-compose.sh').read_text()
        self.assertIn("production deployment is disabled", script)

    def test_script_ships_in_image(self):
        self.assertIn('COPY scripts ./scripts', (ROOT / 'backend/Dockerfile').read_text())


if __name__ == '__main__':
    unittest.main()
