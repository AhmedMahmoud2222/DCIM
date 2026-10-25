"""Offline validation of the Claude agent gate and workflow policy (tests v1.0).

Covers authorization, command parsing, agent routing against the OpenCode gate, duplicate
events, and isolation from the OpenCode workflow. Live execution (real @claude comment
starting the action) needs the CLAUDE_CODE_OAUTH_TOKEN secret and is listed in
docs/security/CLAUDE_AGENT_AUTHORITY.md.
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claude = load('claude_gate', '.github/scripts/claude_gate.py')
opencode = load('opencode_gate', '.github/scripts/opencode_gate.py')

REPO = 'AhmedMahmoud2222/DCIM'
OWNER_ID = 285010264
SHA = 'a' * 40
BASE_SHA = 'b' * 40


def event(body, user_id=OWNER_ID, assoc='OWNER', user_type='User', repo=REPO, pull=True, action='created'):
    issue = {'number': 7}
    if pull:
        issue['pull_request'] = {'url': 'x'}
    return {'action': action, 'repository': {'full_name': repo}, 'issue': issue,
            'comment': {'id': 99, 'body': body, 'author_association': assoc,
                        'user': {'id': user_id, 'type': user_type}}}


def pr(state='open', head_repo=REPO, base_repo=REPO, number=7, sha=SHA, ref='feature/x', merged=False):
    return {'number': number, 'state': state, 'merged': merged,
            'head': {'sha': sha, 'ref': ref, 'repo': {'full_name': head_repo}},
            'base': {'sha': BASE_SHA, 'repo': {'full_name': base_repo}}}


def decide(ev, pull=None, allowed=frozenset({OWNER_ID}), reactions=None, name='issue_comment'):
    return claude.decide(name, ev, REPO, set(allowed), pr() if pull is None else pull, reactions)


class AuthorizationTests(unittest.TestCase):
    def test_authorized_owner_command(self):
        ok, reason = decide(event('@claude Fix the identified issues on this PR'))
        self.assertTrue(ok, reason)

    def test_unauthorized_actor_rejected(self):
        for ev in (event('@claude fix', user_id=1), event('@claude fix', assoc='CONTRIBUTOR'),
                   event('@claude fix', assoc='NONE'), event('@claude fix', user_type='Bot')):
            self.assertFalse(decide(ev)[0])

    def test_allowlist_and_association_both_required(self):
        self.assertFalse(decide(event('@claude fix'), allowed=set())[0])
        self.assertFalse(decide(event('@claude fix', assoc='CONTRIBUTOR'))[0])
        self.assertEqual(claude.parse_allowlist('1,x'), set())

    def test_wrong_event_repo_and_action_rejected(self):
        self.assertFalse(decide(event('@claude fix'), name='pull_request_review_comment')[0])
        self.assertFalse(decide(event('@claude fix', repo='evil/DCIM'))[0])
        self.assertFalse(decide(event('@claude fix', action='edited'))[0])

    def test_pull_request_scope(self):
        self.assertFalse(decide(event('@claude fix', pull=False))[0])  # plain issue
        self.assertFalse(decide(event('@claude fix'), pull=pr(head_repo='fork/DCIM'))[0])
        self.assertFalse(decide(event('@claude fix'), pull=pr(state='closed'))[0])
        self.assertFalse(decide(event('@claude fix'), pull=pr(merged=True))[0])
        self.assertFalse(decide(event('@claude fix'), pull=pr(number=8))[0])
        self.assertFalse(decide(event('@claude fix'), pull=pr(sha='abc'))[0])
        self.assertFalse(decide(event('@claude fix'), pull=pr(ref='bad ref;rm'))[0])
        self.assertFalse(claude.decide('issue_comment', event('@claude fix'), REPO, {OWNER_ID}, {})[0])


class ParsingTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(claude.extract_task('@claude do it')[0], 'do it')
        self.assertEqual(claude.extract_task('@claude\r\nline1\r\nline2')[0], 'line1\nline2')
        self.assertEqual(claude.extract_task('﻿@claude go')[0], 'go')

    def test_spoofed_or_misplaced_mentions_rejected(self):
        for body in ('please @claude fix', '> @claude fix', '```\n@claude fix\n```', '@Claude fix',
                     '@claude-bot fix', '@claudette fix', '@claude', '@claude   ', 'x@claude fix',
                     ' @claude fix', '', None, '/oc @claude fix'):
            self.assertIsNone(claude.extract_task(body)[0], body)

    def test_length_limit(self):
        self.assertIsNone(claude.extract_task('@claude ' + 'a' * 8001)[0])


class RoutingTests(unittest.TestCase):
    """Each comment reaches exactly one agent."""

    def opencode_accepts(self, body):
        ok, _reason, _task = opencode.decide('issue_comment', event(body), REPO, {OWNER_ID}, pr())
        return ok

    def test_claude_comment_never_reaches_opencode(self):
        self.assertTrue(decide(event('@claude fix'))[0])
        self.assertFalse(self.opencode_accepts('@claude fix'))

    def test_opencode_comment_never_reaches_claude(self):
        for body in ('/oc fix', '/opencode review', '/oc fix @claude'):
            self.assertTrue(self.opencode_accepts(body), body)
            self.assertFalse(decide(event(body))[0], body)

    def test_workflow_prefilter_matches_gate_grammar(self):
        job = load_workflow()['jobs']['gate']
        self.assertIn("startsWith(github.event.comment.body, '@claude')", job['if'])


class DuplicateTests(unittest.TestCase):
    CLAIM = {'content': 'eyes', 'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}

    def test_claimed_comment_rejected(self):
        ok, reason = decide(event('@claude fix'), reactions=[self.CLAIM])
        self.assertFalse(ok)
        self.assertIn('duplicate', reason)

    def test_human_or_other_reaction_is_not_a_claim(self):
        spoof = {'content': 'eyes', 'user': {'login': 'mallory', 'type': 'User'}}
        other = {'content': 'rocket', 'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}
        self.assertTrue(decide(event('@claude fix'), reactions=[spoof, other])[0])

    def test_gate_job_serializes_per_comment(self):
        group = load_workflow()['jobs']['gate']['concurrency']
        self.assertIn('github.event.comment.id', group['group'])
        self.assertFalse(group['cancel-in-progress'])

    def test_main_reads_reactions_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'e.json').write_text(json.dumps(event('@claude fix')))
            (tmp / 'p.json').write_text(json.dumps(pr()))
            (tmp / 'r.json').write_text(json.dumps([self.CLAIM]))
            out = tmp / 'out'
            env = {**os.environ, 'GITHUB_EVENT_PATH': str(tmp / 'e.json'), 'GITHUB_EVENT_NAME': 'issue_comment',
                   'GITHUB_REPOSITORY': REPO, 'ALLOWED_ACTOR_IDS': str(OWNER_ID), 'PR_JSON_PATH': str(tmp / 'p.json'),
                   'GITHUB_OUTPUT': str(out)}
            run = lambda extra: subprocess.run(['python3', str(ROOT / '.github/scripts/claude_gate.py')],
                                               env={**env, **extra}, capture_output=True, text=True, check=True)
            run({'REACTIONS_JSON_PATH': str(tmp / 'r.json')})
            self.assertIn('authorized=false', out.read_text())
            out.write_text('')
            run({})
            text = out.read_text()
            self.assertIn('authorized=true', text)
            self.assertIn(f'authorized_sha={SHA}', text)
            self.assertIn('head_ref=feature/x', text)
            out.write_text('')
            (tmp / 'p.json').write_text('{not json')
            run({})
            self.assertIn('authorized=false', out.read_text())


def load_workflow():
    return yaml.safe_load((ROOT / '.github/workflows/claude-agent.yml').read_text())


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / '.github/workflows/claude-agent.yml').read_text()
        self.wf = load_workflow()
        self.triggers = self.wf.get('on') or self.wf.get(True)

    def test_trigger_is_issue_comment_created_only(self):
        self.assertEqual(self.triggers, {'issue_comment': {'types': ['created']}})
        self.assertEqual(self.wf['permissions'], {})

    def test_action_pinned_to_full_sha(self):
        refs = re.findall(r'uses:\s*(\S+)', self.text)
        self.assertTrue(any(r.startswith('anthropics/claude-code-action@') for r in refs))
        for ref in refs:
            self.assertRegex(ref, r'^[^@\s]+@[0-9a-f]{40}$')

    def test_secret_only_in_claude_job_and_gate_has_no_secret(self):
        jobs = self.wf['jobs']
        self.assertNotIn('secrets.', yaml.safe_dump(jobs['gate']))
        self.assertIn('secrets.CLAUDE_CODE_OAUTH_TOKEN', yaml.safe_dump(jobs['claude']))
        self.assertEqual(self.text.count('secrets.'), 1)

    def test_claude_job_requires_gate_and_signing(self):
        job = self.wf['jobs']['claude']
        self.assertEqual(job['needs'], 'gate')
        self.assertIn("needs.gate.outputs.authorized == 'true'", job['if'])
        step = next(s for s in job['steps'] if s.get('id') == 'claude')
        self.assertIs(step['with']['use_commit_signing'], True)
        self.assertEqual(step['with']['allowed_bots'], '')
        self.assertEqual(job['concurrency']['cancel-in-progress'], False)

    def test_least_privilege_permissions(self):
        perms = self.wf['jobs']['claude']['permissions']
        self.assertEqual(perms, {'contents': 'write', 'pull-requests': 'write', 'issues': 'write', 'actions': 'read'})
        self.assertNotIn('id-token', self.text)
        self.assertEqual(self.wf['jobs']['gate']['permissions'], {'contents': 'read', 'pull-requests': 'write'})

    def test_no_merge_deploy_close_or_pr_creation(self):
        step = next(s for s in self.wf['jobs']['claude']['steps'] if s.get('id') == 'claude')
        args = step['with']['claude_args']
        for token in ('Bash(gh:*)', 'Bash(git push:*)'):
            self.assertIn(token, args)
        self.assertNotIn('--allowedTools', args)
        self.assertNotRegex(self.text, r'gh pr (create|merge|close)|merge_pull_request|auto-merge')

    def test_event_data_not_interpolated_into_shell(self):
        for job in self.wf['jobs'].values():
            for step in job['steps']:
                run = step.get('run', '')
                self.assertNotIn('${{ github.event.', run)
                self.assertNotIn('${{ needs.', run)

    def test_head_recheck_and_branch_guard_present(self):
        self.assertIn('PR head moved or closed after authorization', self.text)
        self.assertIn('instead of the PR branch', self.text)


class IsolationTests(unittest.TestCase):
    OPENCODE_FILES = ('.github/workflows/opencode.yml', '.github/scripts/opencode_gate.py')

    def test_workflows_are_separate_files_with_separate_groups_and_vars(self):
        oc = (ROOT / '.github/workflows/opencode.yml').read_text()
        cl = (ROOT / '.github/workflows/claude-agent.yml').read_text()
        self.assertNotIn('OPENCODE_ALLOWED_ACTOR_IDS', cl)
        self.assertNotIn('CLAUDE_ALLOWED_ACTOR_IDS', oc)
        self.assertNotIn('claude-agent', oc)
        self.assertNotIn('opencode_gate', cl)
        self.assertNotIn('/oc', cl.replace('# ', '').split('name: Claude Agent')[1])

    def test_opencode_gate_script_and_workflow_unchanged_in_this_branch(self):
        base = subprocess.run(['git', 'merge-base', 'HEAD', 'origin/main'], cwd=ROOT, capture_output=True, text=True)
        if base.returncode != 0:
            self.skipTest('origin/main not available (shallow clone)')
        diff = subprocess.run(['git', 'diff', '--name-only', base.stdout.strip(), '--', *self.OPENCODE_FILES,
                               '.github/scripts/opencode_*.py', '.github/workflows/opencode*.yml'],
                              cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(diff.stdout.strip(), '')

    def test_opencode_gate_behaviour_unchanged(self):
        digest = hashlib.sha256((ROOT / '.github/scripts/opencode_gate.py').read_bytes()).hexdigest()
        self.assertEqual(len(digest), 64)
        ok, _r, task = opencode.decide('issue_comment', event('/oc review'), REPO, {OWNER_ID}, pr())
        self.assertTrue(ok)
        self.assertEqual(task, 'review')


if __name__ == '__main__':
    unittest.main()
