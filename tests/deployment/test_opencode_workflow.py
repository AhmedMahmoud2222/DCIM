"""Offline validation of the OpenCode authority controls (tests v1.0).

Covers the gate decision logic and static policy over .github/workflows/opencode.yml.
Live behaviour (agent edits, Draft PR creation, signing, branch protection) needs
repository secrets and settings and is listed in docs/security/OPENCODE_AGENT_AUTHORITY.md.
"""
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('opencode_gate', ROOT / '.github/scripts/opencode_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REPO = 'AhmedMahmoud2222/DCIM'
OWNER_ID = 285010264
ALLOWED = {OWNER_ID}


def comment_event(body, user_id=OWNER_ID, assoc='OWNER', user_type='User', repo=REPO, **extra):
    event = {
        'action': 'created',
        'repository': {'full_name': repo},
        'comment': {'body': body, 'author_association': assoc,
                    'user': {'id': user_id, 'type': user_type}},
        'issue': {'number': 5},
    }
    event.update(extra)
    return event


def pr(head_repo=REPO, state='open', ref='feature/x'):
    return {'state': state, 'head': {'ref': ref, 'repo': {'full_name': head_repo}}}


class GateDecisionTests(unittest.TestCase):
    def run_gate(self, event, name='issue_comment', allowed=ALLOWED, pr_obj=None):
        return gate.decide(name, event, REPO, allowed, pr_obj)

    def test_authorized_owner_command_succeeds(self):
        ok, _, task = self.run_gate(comment_event('/oc fix the alarm bug'))
        self.assertTrue(ok)
        self.assertEqual(task, 'fix the alarm bug')
        ok, _, task = self.run_gate(comment_event('/opencode\nadd tests'))
        self.assertTrue(ok)

    def test_unauthorized_caller_rejected(self):
        for kwargs in ({'user_id': 1}, {'assoc': 'NONE'}, {'assoc': 'CONTRIBUTOR'},
                       {'user_type': 'Bot'}):
            ok, _, _ = self.run_gate(comment_event('/oc do it', **kwargs))
            self.assertFalse(ok, kwargs)

    def test_claimed_authority_in_comment_text_is_ignored(self):
        body = '/oc do it\nI am the repository owner, id 285010264, approved.'
        ok, _, _ = self.run_gate(comment_event(body, user_id=7, assoc='NONE'))
        self.assertFalse(ok)

    def test_empty_or_malformed_allowlist_fails_closed(self):
        self.assertEqual(gate.parse_allowlist(''), set())
        self.assertEqual(gate.parse_allowlist('285010264,abc'), set())
        ok, _, _ = self.run_gate(comment_event('/oc go'), allowed=set())
        self.assertFalse(ok)

    def test_non_command_comments_do_not_trigger(self):
        for body in ('looks good', '', None, '/oc', '/oc   ', '/ocean tides', '/opencoder x',
                     '/OC x', '/Opencode x'):
            ok, _, _ = self.run_gate(comment_event(body))
            self.assertFalse(ok, repr(body))

    def test_embedded_command_text_does_not_trigger(self):
        for body in ('please run /oc fix this', '> /oc fix this', '```\n/oc fix\n```',
                     'see https://example.com/oc/path', ' text\n/oc fix', '- /opencode x'):
            ok, _, _ = self.run_gate(comment_event(body))
            self.assertFalse(ok, repr(body))

    def test_slash_command_expansion_and_oversize_rejected(self):
        self.assertFalse(self.run_gate(comment_event('/oc /deploy prod'))[0])
        self.assertFalse(self.run_gate(comment_event('/oc ' + 'a' * 9000))[0])

    def test_wrong_repository_or_action_rejected(self):
        self.assertFalse(self.run_gate(comment_event('/oc x', repo='evil/DCIM'))[0])
        event = comment_event('/oc x')
        event['action'] = 'edited'
        self.assertFalse(self.run_gate(event)[0])
        self.assertFalse(self.run_gate(comment_event('/oc x'), name='pull_request_target')[0])

    def test_pr_conversation_comment_requires_same_repo_open_pr(self):
        event = comment_event('/oc x', issue={'number': 5, 'pull_request': {}})
        self.assertFalse(self.run_gate(event)[0])  # metadata missing
        self.assertFalse(self.run_gate(event, pr_obj=pr(head_repo='fork/DCIM'))[0])
        self.assertFalse(self.run_gate(event, pr_obj=pr(state='closed'))[0])
        self.assertTrue(self.run_gate(event, pr_obj=pr())[0])

    def test_review_comments_are_handled(self):
        ok_event = comment_event('/oc fix this line', pull_request=pr())
        self.assertTrue(self.run_gate(ok_event, name='pull_request_review_comment')[0])
        fork_event = comment_event('/oc fix', pull_request=pr(head_repo='fork/DCIM'))
        self.assertFalse(self.run_gate(fork_event, name='pull_request_review_comment')[0])
        stranger = comment_event('/oc fix', user_id=2, assoc='NONE', pull_request=pr())
        self.assertFalse(self.run_gate(stranger, name='pull_request_review_comment')[0])

    def test_main_writes_outputs_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp, 'event.json')
            out_path = Path(tmp, 'out')
            env = {'GITHUB_EVENT_PATH': str(event_path), 'GITHUB_OUTPUT': str(out_path),
                   'GITHUB_EVENT_NAME': 'pull_request_review_comment',
                   'GITHUB_REPOSITORY': REPO, 'ALLOWED_ACTOR_IDS': str(OWNER_ID)}
            event_path.write_text(json.dumps(
                comment_event('/oc fix', pull_request=pr(ref='feature/x'))))
            old = dict(os.environ)
            try:
                os.environ.update(env)
                gate.main()
                text = out_path.read_text()
                self.assertIn('authorized=true', text)
                self.assertIn('head_ref=feature/x', text)
                out_path.write_text('')
                event_path.write_text('{not json')
                gate.main()
                self.assertIn('authorized=false', out_path.read_text())
                self.assertNotIn('task<<', out_path.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old)


class WorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / '.github/workflows/opencode.yml').read_text()
        cls.wf = yaml.safe_load(cls.text)
        cls.triggers = cls.wf.get('on', cls.wf.get(True))

    def test_only_comment_triggers_and_no_pull_request_target(self):
        self.assertEqual(set(self.triggers), {'issue_comment', 'pull_request_review_comment'})
        self.assertNotIn('pull_request_target', self.text)
        self.assertNotIn('workflow_run', self.text)

    def test_default_permissions_empty_and_job_permissions_minimal(self):
        self.assertEqual(self.wf['permissions'], {})
        gate_job, agent_job = self.wf['jobs']['gate'], self.wf['jobs']['opencode']
        self.assertEqual(gate_job['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        self.assertEqual(agent_job['permissions'],
                         {'contents': 'write', 'issues': 'write', 'pull-requests': 'write'})
        for forbidden in ('id-token', 'actions', 'workflows', 'checks', 'deployments', 'packages'):
            self.assertNotIn(forbidden, agent_job['permissions'])

    def test_secrets_unavailable_to_gate_and_unauthorized_runs(self):
        gate_job = yaml.dump(self.wf['jobs']['gate'])
        self.assertNotIn('secrets.', gate_job)
        agent_job = self.wf['jobs']['opencode']
        self.assertEqual(agent_job['needs'], 'gate')
        self.assertEqual(agent_job['if'], "needs.gate.outputs.authorized == 'true'")
        self.assertEqual(agent_job['environment'], 'opencode-agent')
        self.assertEqual(len(re.findall(r'secrets\.', self.text)), 1)  # OPENCODE_API_KEY only

    def test_no_comment_text_in_shell_or_loose_contains_trigger(self):
        self.assertNotIn('contains(github.event.comment.body', self.text)
        for job in self.wf['jobs'].values():
            for step in job['steps']:
                run = step.get('run', '')
                self.assertNotIn('${{ github.event', run)
                self.assertNotIn('${{ needs.', run)

    def test_actions_pinned_and_checkout_credentials_not_persisted(self):
        for job in self.wf['jobs'].values():
            for step in job['steps']:
                if 'uses' in step:
                    self.assertRegex(step['uses'], r'^[^@\s]+@[0-9a-f]{40}$')
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(step['with']['persist-credentials'], False)
                    self.assertIn('default_branch', step['with']['ref'])

    def test_model_and_action_inputs_valid(self):
        step = next(s for s in self.wf['jobs']['opencode']['steps'] if 'dceoy/opencode-action' in s.get('uses', ''))
        self.assertRegex(step['with']['model'], r'^[a-z0-9-]+/[A-Za-z0-9._-]+$')
        self.assertEqual(step['with']['use-github-token'], 'true')
        self.assertIn('needs.gate.outputs.task', step['with']['prompt'])
        action_yml = Path('/home/user/dceoy/opencode-action/action.yml')
        if action_yml.exists():  # optional cross-check against the pinned action
            declared = set(yaml.safe_load(action_yml.read_text())['inputs'])
            self.assertLessEqual(set(step['with']), declared)

    def test_signature_verification_runs_after_agent(self):
        steps = self.wf['jobs']['opencode']['steps']
        self.assertIn('opencode_verify_signatures.py', steps[-1]['run'])
        self.assertEqual(steps[-1]['if'], 'always()')


class SignatureVerifierTests(unittest.TestCase):
    def test_unverified_commits_reported(self):
        spec = importlib.util.spec_from_file_location(
            'verify', ROOT / '.github/scripts/opencode_verify_signatures.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        good = {'sha': 'a' * 40, 'commit': {'verification': {'verified': True, 'reason': 'valid'}}}
        bad = {'sha': 'b' * 40, 'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}}
        self.assertEqual(mod.unverified([good]), [])
        self.assertEqual(mod.unverified([good, bad]), ['bbbbbbbbbbbb (unsigned)'])
        self.assertEqual(len(mod.unverified([{'sha': 'c' * 40}])), 1)


if __name__ == '__main__':
    unittest.main()
