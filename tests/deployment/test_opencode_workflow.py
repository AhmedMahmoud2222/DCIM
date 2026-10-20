"""Offline validation of the OpenCode authorization gate and workflow policy (tests v2.0).

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

    def test_review_comment_events_are_not_privileged(self):
        # pull_request_review_comment runs the workflow file from the PR merge ref,
        # so it must never reach the agent even from the owner.
        event = comment_event('/oc fix this line', pull_request=pr())
        ok, reason, _ = self.run_gate(event, name='pull_request_review_comment')
        self.assertFalse(ok)
        self.assertIn('unsupported event', reason)

    def test_main_writes_outputs_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp, 'event.json')
            out_path = Path(tmp, 'out')
            pr_path = Path(tmp, 'pr.json')
            pr_path.write_text(json.dumps(pr(ref='feature/x')))
            env = {'GITHUB_EVENT_PATH': str(event_path), 'GITHUB_OUTPUT': str(out_path),
                   'GITHUB_EVENT_NAME': 'issue_comment', 'PR_JSON_PATH': str(pr_path),
                   'GITHUB_REPOSITORY': REPO, 'ALLOWED_ACTOR_IDS': str(OWNER_ID)}
            event_path.write_text(json.dumps(
                comment_event('/oc fix', issue={'number': 5, 'pull_request': {}})))
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
        cls.jobs = cls.wf['jobs']

    def steps_of(self, job):
        return self.jobs[job]['steps']

    def test_only_issue_comment_trigger(self):
        self.assertEqual(set(self.triggers), {'issue_comment'})
        for banned in ('pull_request_target', 'workflow_run', 'pull_request_review_comment:'):
            self.assertNotIn(banned, self.text)

    def test_permissions_are_minimal_per_job(self):
        self.assertEqual(self.wf['permissions'], {})
        self.assertEqual(self.jobs['gate']['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        self.assertEqual(self.jobs['opencode']['permissions'],
                         {'contents': 'read', 'issues': 'write', 'pull-requests': 'write'})
        self.assertEqual(self.jobs['publish']['permissions'], {'contents': 'read'})
        for job in self.jobs.values():
            for forbidden in ('id-token', 'actions', 'workflows', 'administration', 'deployments'):
                self.assertNotIn(forbidden, job['permissions'])

    def test_agent_job_cannot_write_repository_contents(self):
        self.assertEqual(self.jobs['opencode']['permissions']['contents'], 'read')
        agent_job = yaml.dump(self.jobs['opencode'])
        self.assertNotIn('APP_PRIVATE_KEY', agent_job)
        self.assertNotIn('create-github-app-token', agent_job)

    def test_secret_scoping(self):
        self.assertNotIn('secrets.', yaml.dump(self.jobs['gate']))
        self.assertEqual(self.jobs['opencode']['environment'], 'opencode-agent')
        self.assertEqual(self.jobs['publish']['environment'], 'opencode-publish')
        self.assertEqual(sorted(set(re.findall(r'secrets\.([A-Z_]+)', yaml.dump(self.jobs['opencode'])))),
                         ['OPENCODE_API_KEY'])
        self.assertEqual(sorted(set(re.findall(r'secrets\.([A-Z_]+)', yaml.dump(self.jobs['publish'])))),
                         ['OPENCODE_APP_PRIVATE_KEY'])
        self.assertNotIn('DCIM_GIT_SIGNING_KEY', self.text)

    def test_job_ordering_and_authorization_condition(self):
        self.assertEqual(self.jobs['opencode']['needs'], 'gate')
        self.assertEqual(self.jobs['opencode']['if'], "needs.gate.outputs.authorized == 'true'")
        self.assertEqual(self.jobs['publish']['needs'], ['gate', 'opencode'])
        self.assertIn("needs.opencode.result == 'success'", self.jobs['publish']['if'])

    def test_app_token_is_repo_scoped_and_least_privilege(self):
        step = next(s for s in self.steps_of('publish') if 'create-github-app-token' in s.get('uses', ''))
        with_ = step['with']
        self.assertEqual(with_['repositories'], '${{ github.event.repository.name }}')
        granted = {k for k in with_ if k.startswith('permission-')}
        self.assertEqual(granted, {'permission-contents', 'permission-pull-requests', 'permission-issues'})
        self.assertEqual({with_[k] for k in granted}, {'write'})
        self.assertNotIn('permission-workflows', with_)
        self.assertNotIn('permission-administration', with_)

    def test_agent_never_sees_publish_credentials(self):
        env_text = yaml.dump([s.get('env', {}) for s in self.steps_of('opencode')])
        self.assertNotIn('GH_PUBLISH_TOKEN', env_text)
        self.assertNotIn('app.outputs.token', env_text)

    def test_no_untrusted_text_in_shell(self):
        self.assertNotIn('contains(github.event.comment.body', self.text)
        for job in self.jobs.values():
            for step in job['steps']:
                run = step.get('run', '')
                for banned in ('${{ github.event', '${{ needs.', '${{ steps.', '${{ vars.', '${{ inputs.'):
                    self.assertNotIn(banned, run)

    def test_actions_pinned_and_checkout_hardened(self):
        for job in self.jobs.values():
            for step in job['steps']:
                if 'uses' in step:
                    self.assertRegex(step['uses'], r'^[^@\s]+@[0-9a-f]{40}$')
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(step['with']['persist-credentials'], False)
                    self.assertEqual(step['with']['ref'], '${{ github.sha }}')

    def test_agent_inputs_valid_and_model_comes_from_router(self):
        step = next(s for s in self.steps_of('opencode') if 'dceoy/opencode-action' in s.get('uses', ''))
        self.assertEqual(step['with']['model'], '${{ steps.route.outputs.model }}')
        self.assertEqual(step['with']['use-github-token'], 'true')
        self.assertIn('needs.gate.outputs.task', step['with']['prompt'])
        self.assertTrue(step['continue-on-error'])
        self.assertIn('"*.env":"deny"', step['env']['OPENCODE_CONFIG_CONTENT'])
        action_yml = Path('/home/user/dceoy/opencode-action/action.yml')
        if action_yml.exists():
            self.assertLessEqual(set(step['with']), set(yaml.safe_load(action_yml.read_text())['inputs']))

    def test_no_hardcoded_paid_or_unapproved_model(self):
        self.assertNotRegex(self.text, r'opencode-go/|kimi|anthropic/|openai/')
        self.assertNotIn('model: opencode/', self.text)

    def test_publisher_runs_scripts_from_trusted_checkout_in_clean_job(self):
        names = [s.get('name', '') for s in self.steps_of('publish')]
        self.assertEqual(self.steps_of('publish')[0]['uses'].split('@')[0], 'actions/checkout')
        self.assertTrue(any('re-verify' in n for n in names))
        self.assertIn('opencode_publish.py', yaml.dump(self.steps_of('publish')))


if __name__ == '__main__':
    unittest.main()
