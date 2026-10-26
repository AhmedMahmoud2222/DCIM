import copy
import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.github/scripts'))
p = importlib.import_module('orchestration_policy')
HEAD, BASE = 'a' * 40, 'b' * 40


def facts():
    return dict(head=HEAD, base=BASE, pr=5, implementer='codex', paths=['docs/usage.md'],
                commits=[dict(sha=HEAD, verification=dict(verified=True, reason='valid'))],
                commit_range=dict(base=BASE, head=HEAD, complete=True, commits=[HEAD]),
                ci_passed=True, security_passed=True, regression_passed=True,
                dependencies_accepted=True, migration_compatible=True, protections_satisfied=True,
                conflict_free=True, findings_resolved=True, evidence_complete=True,
                ci_head=HEAD, ci_base=BASE,
                review=dict(head=HEAD, base=BASE, pr=5, reviewer='claude', authenticated=True,
                            acceptance_check=True, comment_id=10))


class PolicyTests(unittest.TestCase):
    def test_eligible_low_risk_evidence(self):
        self.assertEqual(p.merge_blockers(facts()), [])

    def test_every_required_gate_fails_closed(self):
        for field in ('ci_passed', 'security_passed', 'regression_passed', 'dependencies_accepted',
                      'migration_compatible', 'protections_satisfied', 'conflict_free',
                      'findings_resolved', 'evidence_complete'):
            for value in (False, None, 'true', 1):
                f = facts(); f[field] = value
                self.assertIn(field, p.merge_blockers(f))

    def test_signatures_complete_evidence_and_stale_reviews(self):
        for commits in ([], None, [dict(verification=dict(verified=False, reason='valid'))],
                        [dict(verification=dict(verified=True, reason='unknown_key'))]):
            f = facts(); f['commits'] = commits
            self.assertTrue(p.merge_blockers(f))
        for part in ('ci_head', 'ci_base'):
            f = facts(); f[part] = 'c' * 40
            self.assertIn('stale CI', p.merge_blockers(f))
        for part in ('head', 'base'):
            f = facts(); f['review'][part] = 'c' * 40
            self.assertIn('stale independent review', p.merge_blockers(f))

    def test_no_self_acceptance_or_text_only_review(self):
        for changes in (dict(reviewer='codex'), dict(authenticated=False),
                        dict(acceptance_check=False), dict(comment_id=None), dict(pr=6)):
            f = facts(); f['review'].update(changes)
            self.assertTrue(p.merge_blockers(f))

    def test_signed_commit_must_belong_to_complete_current_range(self):
        for change in ({'base': HEAD}, {'head': BASE}, {'commits': []}, {'complete': False}):
            f = facts(); f['commit_range'].update(change)
            self.assertTrue(p.merge_blockers(f))

    def test_signature_reason_is_independently_required(self):
        f = facts()
        f['commits'][0]['verification']['reason'] = 'unknown_key'
        self.assertIn('unsigned or invalid commit', p.merge_blockers(f))
        for sha in (None, 'c' * 40, BASE):
            f = facts(); f['commits'][0]['sha'] = sha
            self.assertTrue(p.merge_blockers(f))

    def test_risk_never_guessed_low(self):
        for path in ('.github/workflows/ci.yml', 'backend/app/auth.py', 'backend/migrations/0058.py',
                     'docs/security.md', 'scripts/deploy.sh', 'unknown', '../docs/usage.md',
                     'edge_collector/power.py', 'docker-compose.yml'):
            self.assertEqual(p.risk([path]), 'high', path)
        self.assertEqual(p.risk(['frontend/src/widget.tsx']), 'medium')
        f = facts(); f['paths'] = ['backend/app/widget.py']
        self.assertTrue(p.merge_blockers(f))

    def test_fallback_requires_fresh_observed_capability(self):
        observations = {a: dict(ready=True, checked_at=100, capabilities=['implement', 'review'])
                        for a in p.AGENTS}
        self.assertEqual(p.select_agent(observations, {}, 'implement', 101), 'opencode')
        self.assertEqual(p.select_agent(observations, {'opencode': 3}, 'review', 101,
                                        implementer='claude'), 'codex')
        for obs in ({}, {a: dict(o, ready=False) for a, o in observations.items()}, observations):
            with self.assertRaises(p.PolicyError):
                p.select_agent(obs, {}, 'review', 1000)

    def test_lifecycle_idempotency_cas_and_audit(self):
        task = p.new_task(1, 5, HEAD, BASE, 100)
        assigned = p.transition(task, 'ASSIGNED', 'e1', 0, facts(), 101)
        self.assertEqual(assigned['implementer'], 'codex')
        self.assertEqual(p.transition(assigned, 'ASSIGNED', 'e1', 0, facts(), 102), assigned)
        with self.assertRaises(p.PolicyError):
            p.transition(assigned, 'ASSIGNED', 'e1', 1, {}, 102)
        with self.assertRaises(p.PolicyError):
            p.transition(assigned, 'IMPLEMENTING', 'e2', 0, facts(), 102)
        self.assertNotEqual(assigned['audit_hash'], task['audit_hash'])
        self.assertEqual(task['revision'], 0)

    def test_changed_head_restarts_ci_and_retry_limit_escalates(self):
        task = p.new_task(1, 5, HEAD, BASE, 100)
        task = p.transition(task, 'ASSIGNED', '1', 0, facts(), 101)
        changed = facts(); changed['head'] = 'c' * 40
        task = p.transition(task, 'IMPLEMENTING', '2', 1, changed, 102)
        self.assertEqual(task['state'], 'CI')
        task['attempts'] = 3
        task = p.transition(task, 'REMEDIATION', '3', 2, changed, 103)
        self.assertEqual(task['state'], 'ESCALATED')

    def test_timeout_and_illegal_transition(self):
        task = p.new_task(1, 5, HEAD, BASE, 100)
        with self.assertRaises(p.PolicyError):
            p.transition(task, 'MERGED', 'x', 0, facts(), 101)
        task = p.transition(task, 'ASSIGNED', 'x', 0, facts(), 4000)
        self.assertEqual(task['state'], 'ESCALATED')

    def test_intermediate_events_do_not_extend_execution_deadline(self):
        task = p.new_task(1, 5, HEAD, BASE, 100)
        task = p.transition(task, 'ASSIGNED', 'first', 0, facts(), 3500)
        task = p.transition(task, 'IMPLEMENTING', 'second', 1, facts(), 4000)
        self.assertEqual(task['state'], 'ESCALATED')

    def test_deployment_remains_inactive_even_with_claimed_success(self):
        task = p.new_task(1, 5, HEAD, BASE, 100)
        task['state'] = 'MERGED'
        f = facts(); f.update(deployed=True, healthy=True, production_enabled=True)
        with self.assertRaisesRegex(p.PolicyError, 'not activated'):
            p.transition(task, 'STAGING', 'x', 0, f, 101)
        for state in ('STAGING', 'PRODUCTION'):
            task['state'] = state
            with self.assertRaises(p.PolicyError):
                p.transition(task, 'ROLLED_BACK', 'x', 0, f, 101)
            f['rollback_verified'] = True
            result = p.transition(task, 'ROLLED_BACK', 'x', 0, f, 101)
            self.assertEqual(result['state'], 'ROLLED_BACK')
            f.pop('rollback_verified')
