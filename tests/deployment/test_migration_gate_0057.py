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
        import os
        os.chdir(ROOT / 'backend')
        try:
            pending = gate.pending_revisions(['0056_pdu_vertical_placement'])
            self.assertIn(gate.GATED_REVISION, pending)
            self.assertNotIn('0056_pdu_vertical_placement', pending)
            self.assertNotIn(gate.GATED_REVISION, gate.pending_revisions([gate.GATED_REVISION]))
            self.assertIn(gate.GATED_REVISION, gate.pending_revisions([]))
        finally:
            os.chdir(ROOT)


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
