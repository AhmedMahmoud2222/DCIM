"""Offline validation of the OpenCode authorization gate and workflow policy (tests v3.0).

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


HEAD = 'c' * 40


def pr(head_repo=REPO, state='open', ref='feature/x', number=5, base_repo=REPO, merged=False, sha=HEAD):
    return {'number': number, 'state': state, 'merged': merged,
            'base': {'repo': {'full_name': base_repo}},
            'head': {'ref': ref, 'sha': sha, 'repo': {'full_name': head_repo}}}


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

    def test_pr_binding_rejects_mismatches(self):
        event = comment_event('/oc x', issue={'number': 5, 'pull_request': {}})
        for bad in (pr(number=6), pr(merged=True), pr(base_repo='evil/DCIM'), pr(sha='not-a-sha'),
                    pr(sha='C' * 40)):
            self.assertFalse(self.run_gate(event, pr_obj=bad)[0], bad)

    def test_binding_fixes_trusted_baseline_at_authorization(self):
        event = comment_event('/oc x', issue={'number': 5, 'pull_request': {}})
        target = gate.binding(event, pr(), 'd' * 40)
        self.assertEqual(target, {'kind': 'pr', 'number': 5, 'authorized_sha': HEAD, 'head_ref': 'feature/x'})
        target = gate.binding(comment_event('/oc x'), None, 'd' * 40)
        self.assertEqual(target, {'kind': 'issue', 'number': 5, 'authorized_sha': 'd' * 40, 'head_ref': ''})

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
                   'GITHUB_REPOSITORY': REPO, 'ALLOWED_ACTOR_IDS': str(OWNER_ID), 'GITHUB_SHA': 'd' * 40}
            event_path.write_text(json.dumps(
                comment_event('/oc fix', issue={'number': 5, 'pull_request': {}})))
            old = dict(os.environ)
            try:
                os.environ.update(env)
                gate.main()
                text = out_path.read_text()
                for expected in ('authorized=true', 'kind=pr', 'number=5', f'authorized_sha={HEAD}',
                                 'head_ref=feature/x'):
                    self.assertIn(expected, text)
                out_path.write_text('')
                event_path.write_text(json.dumps(comment_event('/oc fix')))
                gate.main()
                self.assertIn(f"authorized_sha={'d' * 40}", out_path.read_text())
                out_path.write_text('')
                os.environ['GITHUB_SHA'] = 'not-a-sha'  # baseline cannot be established: fail closed
                gate.main()
                self.assertIn('authorized=false', out_path.read_text())
                self.assertNotIn('task<<', out_path.read_text())
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
        self.assertEqual(self.jobs['opencode']['permissions'], {'contents': 'read'})
        self.assertEqual(self.jobs['publish']['permissions'], {'contents': 'read'})
        self.assertEqual(self.jobs['report']['permissions'], {})
        for job in self.jobs.values():
            for forbidden in ('id-token', 'actions', 'workflows', 'administration', 'deployments'):
                self.assertNotIn(forbidden, job['permissions'])

    def test_untrusted_generation_job_has_no_github_write_rights(self):
        job = self.jobs['opencode']
        self.assertEqual(job['permissions'], {'contents': 'read'})
        text = yaml.dump(job)
        for banned in ('APP_PRIVATE_KEY', 'create-github-app-token', 'github.token', 'GITHUB_TOKEN',
                       'GH_TOKEN', 'issues: write', 'gh issue', 'gh pr'):
            self.assertNotIn(banned, text)

    def test_secret_scoping(self):
        self.assertNotIn('secrets.', yaml.dump(self.jobs['gate']))
        self.assertEqual(self.jobs['opencode']['environment'], 'opencode-agent')
        self.assertEqual(self.jobs['publish']['environment'], 'opencode-publish')
        self.assertEqual(self.jobs['report']['environment'], 'opencode-publish')
        def used(job):
            return sorted(set(re.findall(r'secrets\.([A-Z_]+)', yaml.dump(self.jobs[job]))))
        self.assertEqual(used('opencode'), ['OPENCODE_API_KEY'])
        self.assertEqual(used('publish'), ['OPENCODE_APP_PRIVATE_KEY'])
        self.assertEqual(used('report'), ['OPENCODE_APP_PRIVATE_KEY'])
        self.assertNotIn('DCIM_GIT_SIGNING_KEY', self.text)

    def test_job_ordering_and_authorization_condition(self):
        self.assertEqual(self.jobs['opencode']['needs'], 'gate')
        self.assertEqual(self.jobs['opencode']['if'], "needs.gate.outputs.authorized == 'true'")
        self.assertEqual(self.jobs['publish']['needs'], ['gate', 'opencode'])
        condition = self.jobs['publish']['if']
        for required in ("needs.opencode.result == 'success'", "outputs.generation == 'success'",
                         "outputs.has_changes == 'true'"):
            self.assertIn(required, condition)
        self.assertIn("needs.gate.outputs.authorized == 'true'", self.jobs['report']['if'])

    def test_concurrency_cannot_be_displaced_by_unauthorized_comments(self):
        self.assertNotIn('concurrency', self.wf)
        self.assertNotIn('concurrency', self.jobs['gate'])
        for name in ('opencode', 'publish'):
            self.assertIn('group', self.jobs[name]['concurrency'])
            self.assertIs(self.jobs[name]['concurrency']['cancel-in-progress'], False)

    def test_app_tokens_are_repo_scoped_and_least_privilege(self):
        for job, expected in (('publish', {'permission-contents', 'permission-pull-requests', 'permission-issues'}),
                              ('report', {'permission-issues'})):
            step = next(s for s in self.steps_of(job) if 'create-github-app-token' in s.get('uses', ''))
            with_ = step['with']
            self.assertEqual(with_['repositories'], '${{ github.event.repository.name }}')
            self.assertEqual({k for k in with_ if k.startswith('permission-')}, expected)
            self.assertEqual({with_[k] for k in expected}, {'write'})

    def test_publisher_target_identity_comes_from_gate_not_artifact(self):
        env = next(s for s in self.steps_of('publish') if s.get('id') == 'publish')['env']
        for key, source in (('AUTHORIZED_SHA', 'needs.gate.outputs.authorized_sha'),
                            ('TARGET_NUMBER', 'needs.gate.outputs.number'),
                            ('EVENT_KIND', 'needs.gate.outputs.kind'),
                            ('PR_HEAD_REF', 'needs.gate.outputs.head_ref'),
                            ('GENERATION_STATUS', 'needs.opencode.outputs.generation')):
            self.assertIn(source, env[key])

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

    def test_agent_runs_in_the_sandbox_with_no_third_party_action(self):
        self.assertNotIn('dceoy/opencode-action', self.text)
        steps = {s.get('name'): s for s in self.steps_of('opencode')}
        run = steps['Run agent (loopback-only, credential held by proxy)']['run']
        self.assertIn('--uid-owner ocagent', run)
        self.assertLess(run.index('still has network egress'), run.index('opencode_guard.py'))
        self.assertIn('unset OPENCODE_API_KEY', run)
        self.assertLess(run.index('opencode_proxy.py'), run.index('unset OPENCODE_API_KEY'))
        self.assertLess(run.index('unset OPENCODE_API_KEY'), run.index('opencode_guard.py'))
        self.assertIn('/etc/opencode/opencode.json', run)
        self.assertIn('chmod 700 "${HOME}"', steps['Create isolated agent user']['run'])
        order = [s.get('name') for s in self.steps_of('opencode')]
        self.assertLess(order.index('Read live model catalog'), order.index('Select model'))
        self.assertLess(order.index('Select model'), order.index('Build sanitized sandbox copy'))
        self.assertLess(order.index('Build sanitized sandbox copy'),
                        order.index('Run agent (loopback-only, credential held by proxy)'))

    def test_collection_and_upload_only_after_verified_generation(self):
        steps = {s.get('name'): s for s in self.steps_of('opencode')}
        self.assertEqual(steps['Collect changes as data']['if'], "steps.agent.outputs.generation == 'success'")
        self.assertIn("has_changes == 'true'", steps['Upload changes']['if'])
        self.assertNotIn('always()', yaml.dump(steps['Collect changes as data']))
        self.assertNotIn('continue-on-error', yaml.dump(self.jobs['opencode']))

    def test_no_hardcoded_paid_or_unapproved_model(self):
        self.assertNotRegex(self.text, r'opencode-go/|kimi|anthropic/|openai/')
        self.assertNotIn('model: opencode/', self.text)

    def test_publisher_runs_scripts_from_trusted_checkout_in_clean_job(self):
        names = [s.get('name', '') for s in self.steps_of('publish')]
        self.assertEqual(self.steps_of('publish')[0]['uses'].split('@')[0], 'actions/checkout')
        self.assertTrue(any('re-verify' in n for n in names))
        self.assertIn('opencode_publish.py', yaml.dump(self.steps_of('publish')))

    def test_job_timeout_preserved(self):
        self.assertEqual(self.jobs['opencode']['timeout-minutes'], 35)


if __name__ == '__main__':
    unittest.main()
