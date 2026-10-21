"""OpenCode manifest, publisher and signature-verifier tests (tests v2.0). No network."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'


def load(name):
    # One shared module instance per script (as in production): exception classes must be identical
    # across the publisher, the review module and these tests.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


collect = load('opencode_collect')
publish = load('opencode_publish')
sandbox = load('opencode_sandbox')
verify = load('opencode_verify_signatures')
BASE = 'a' * 40
NEW = 'b' * 40
MOVED = 'c' * 40
BOT = 'dcim-opencode-agent[bot]'
ENV = {
    'GITHUB_REPOSITORY': 'AhmedMahmoud2222/DCIM', 'DEFAULT_BRANCH': 'main', 'EVENT_KIND': 'issue',
    'TARGET_NUMBER': '7', 'AUTHORIZED_SHA': BASE, 'GENERATION_STATUS': 'success',
    'RUN_URL': 'https://github.com/x/y/actions/runs/1', 'RUN_ID': '1', 'RUN_ATTEMPT': '1',
    'ACTOR_LOGIN': 'AhmedMahmoud2222', 'EXPECT_BOT_LOGIN': BOT,
    'TASK': 'fix the alarm bug\nsecond line', 'MODEL': 'opencode/deepseek-v4.1-flash-free',
    'CATEGORY': 'general', 'ROUTE_REASON': 'category general', 'PR_HEAD_REF': '',
    'SESSION_ID': 'ses_impl000111',
}
PR_ENV = {**ENV, 'EVENT_KIND': 'pr', 'TARGET_NUMBER': '9', 'PR_HEAD_REF': 'feature/x'}
PLAN = {'base_sha': BASE, 'additions': [{'path': 'a.txt', 'contents': 'eA=='}], 'deletions': []}


def build_artifact(tmp, changes, **top):
    root = Path(tmp)
    (root / 'files').mkdir(exist_ok=True)
    entries = []
    for index, (path, op, data) in enumerate(changes):
        if op == 'delete':
            entries.append({'path': path, 'op': 'delete'})
            continue
        (root / 'files' / str(index)).write_bytes(data)
        entries.append({'path': path, 'op': op, 'blob': f'files/{index}', 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'version': 2, 'base_sha': BASE, 'changes': entries, 'unsupported': []}
    manifest.update(top)
    return manifest, str(root)


class FakeApi:
    """In-memory GitHub. Tracks refs so staging, update and deletion are observable.

    `ambiguous` maps 'METHOD path-prefix' -> 'applied' | 'not_applied': the call raises an
    AmbiguousApiError and the mutation either did or did not happen.
    """

    def __init__(self, verified=True, login=BOT, branch_tip=BASE, protected=False, compare='behind',
                 default_protected=True, pr=None, issue=None, delete_fails=False, pulls_fails=False,
                 push_fails=False, ambiguous=None, lookup_fails=False, descent=None):
        self.calls, self.verified, self.login = [], verified, login
        self.refs = {'feature/x': branch_tip}
        self.protected, self.compare = protected, compare
        self.default_protected, self.delete_fails = default_protected, delete_fails
        self.pulls_fails, self.push_fails = pulls_fails, push_fails
        self.ambiguous = ambiguous or {}
        self.lookup_fails = lookup_fails
        self.descent = descent or {'status': 'ahead', 'behind_by': 0}
        self.pr = pr if pr is not None else {
            'number': 9, 'state': 'open', 'merged': False, 'base': {'repo': {'full_name': 'AhmedMahmoud2222/DCIM'}},
            'head': {'ref': 'feature/x', 'sha': BASE, 'repo': {'full_name': 'AhmedMahmoud2222/DCIM'}}}
        self.issue = issue if issue is not None else {'number': 7, 'state': 'open'}
        self.pr_state_after_verify = None
        self.before_push = None
        self.reviews = []

    @property
    def branch_tip(self):
        return self.refs.get('feature/x')

    def _maybe_ambiguous(self, method, path):
        for key, outcome in self.ambiguous.items():
            if key == f'{method} {path}' or (key.endswith('*') and f'{method} {path}'.startswith(key[:-1])):
                return outcome
        return None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        outcome = self._maybe_ambiguous(method, path) if method != 'GET' else None
        if method == 'GET' and path == '':
            return {'default_branch': 'main', 'full_name': 'AhmedMahmoud2222/DCIM'}
        if path == 'branches/main':
            return {'protected': self.default_protected, 'commit': {'sha': 'f' * 40}}
        if path.startswith('branches/'):
            return {'protected': self.protected, 'commit': {'sha': self.refs.get(path[len('branches/'):])}}
        if path.startswith('git/ref/heads/'):
            if self.lookup_fails:
                raise publish.AmbiguousApiError('lookup unreachable')
            tip = self.refs.get(path[len('git/ref/heads/'):])
            if tip is None:
                error = publish.PublishError('HTTP 404')
                error.status = 404
                raise error
            return {'object': {'sha': tip}}
        if path.startswith('issues/') and method == 'GET':
            return self.issue
        if path.startswith('pulls/') and method == 'GET' and path.endswith('/reviews?per_page=100'):
            return self.reviews
        if path.startswith('pulls/') and method == 'GET':
            pr = copy.deepcopy(self.pr)
            if self.pr_state_after_verify and any(c[1].startswith('commits/') for c in self.calls):
                pr['state'] = self.pr_state_after_verify
            return pr
        if path.startswith('compare/'):
            if path == f'compare/{BASE}...{NEW}':      # does the verified commit descend from the baseline?
                return dict(self.descent)
            return {'status': self.compare}            # default...baseline reachability
        if path.startswith('commits/'):
            return {'commit': {'verification': {'verified': self.verified, 'reason': 'valid' if self.verified else 'unsigned'}},
                    'author': {'login': self.login}}
        if method == 'POST' and path == 'git/refs':
            if outcome != 'not_applied':
                self.refs[body['ref'][len('refs/heads/'):]] = body['sha']
            if outcome:
                raise publish.AmbiguousApiError('timeout')
            return {}
        if method == 'DELETE' and path.startswith('git/refs/heads/'):
            if self.delete_fails:
                raise publish.PublishError('HTTP 403')
            if outcome != 'not_applied':
                self.refs.pop(path[len('git/refs/heads/'):], None)
            if outcome:
                raise publish.AmbiguousApiError('timeout')
            return {}
        if path == 'pulls' and method == 'POST':
            if self.pulls_fails:
                raise publish.PublishError('HTTP 500')
            return {'number': 12, 'html_url': 'https://github.com/x/y/pull/12'}
        return {}

    def graphql(self, query, variables):
        self.calls.append(('GRAPHQL', 'createCommitOnBranch', variables))
        branch = variables['input']['branch']['branchName']
        outcome = self._maybe_ambiguous('GRAPHQL', 'createCommitOnBranch')
        if outcome != 'not_applied':
            self.refs[branch] = NEW
        if outcome:
            raise publish.AmbiguousApiError('timeout')
        return {'createCommitOnBranch': {'commit': {'oid': NEW}}}

    def push(self, branch, expected, new, staging):
        """Stand-in for git_cas_push: exact-SHA compare-and-swap against the in-memory remote."""
        if self.before_push:
            self.before_push()
        self.calls.append(('PUSH', f'{branch} {expected[:7]}->{new[:7]}', None))
        outcome = self._maybe_ambiguous('PUSH', branch)
        if self.push_fails or self.refs.get(branch) != expected:
            raise publish.PushRejected('exact-SHA update was rejected')
        if outcome != 'not_applied':
            self.refs[branch] = new
        if outcome:
            raise publish.PushRejected('push result unknown')

    def mutations(self):
        return [(m, p) for m, p, _ in self.calls if m not in ('GET',)]

    def methods(self):
        return [(m, p) for m, p, _ in self.calls]


class ManifestValidationTests(unittest.TestCase):
    def check(self, changes, **top):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = build_artifact(tmp, changes, **top)
            return publish.validate_manifest(manifest, root)

    def test_valid_manifest_builds_file_changes(self):
        plan = self.check([('backend/app.py', 'modify', b'print(1)\n'), ('docs/old.md', 'delete', b'')])
        self.assertEqual(plan['deletions'], ['docs/old.md'])
        self.assertEqual(base64.b64decode(plan['additions'][0]['contents']), b'print(1)\n')

    def test_unsafe_paths_rejected(self):
        for path in ('../etc/passwd', '/abs', '.git/config', 'a/.GIT/x', 'a//b', 'a/./b', 'a\\b', 'x\x00y', ''):
            with self.assertRaises(publish.PublishError, msg=path):
                self.check([(path, 'add', b'x')])

    def test_agent_cannot_modify_its_own_controls_or_add_credential_files(self):
        for path in ('.github/workflows/ci.yml', '.github/scripts/opencode_gate.py',
                     '.github/opencode/model-registry.json', '.github/opencode/secret-scan-allowlist.txt',
                     '.github/CODEOWNERS', 'CODEOWNERS', 'docs/security/OPENCODE_AGENT_AUTHORITY.md',
                     'tests/deployment/test_opencode_router.py', 'tests/deployment/mutation_check.py',
                     'tests/deployment/fixtures/opencode_1_19_3/success_text.jsonl', '.github/opencode/runtime/package-lock.json',
                     '.opencode/commands/x.md', 'opencode.json',
                     'sub/AGENTS.md', '.GitHub/Workflows/x.yml', '.claude/settings.json', 'sub/opencode.jsonc',
                     '.env', 'backend/.env.production', 'keys/server.pem', 'id_rsa'):
            with self.assertRaises(publish.PublishError, msg=path):
                self.check([(path, 'modify', b'x')])

    def test_ordinary_code_paths_allowed(self):
        self.check([('backend/app/main.py', 'modify', b'x'), ('frontend/src/a.tsx', 'add', b'y'),
                    ('.github/scripts/clamd_probe.py', 'modify', b'z'), ('.env.example', 'modify', b'A=1')])

    def test_unsupported_changes_and_tampered_blobs_rejected(self):
        with self.assertRaises(publish.PublishError):
            self.check([('a.txt', 'add', b'x')], unsupported=[{'path': 'link', 'reason': 'mode'}])
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = build_artifact(tmp, [('a.txt', 'add', b'x')])
            Path(root, 'files/0').write_bytes(b'tampered')
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)
            manifest['changes'][0]['blob'] = 'files/../manifest.json'
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)

    def test_declared_size_must_match_actual_blob(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = build_artifact(tmp, [('a.txt', 'add', b'hello')])
            manifest['changes'][0]['size'] = 3
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)

    def test_size_and_count_limits(self):
        with self.assertRaises(publish.PublishError):
            self.check([(f'f{i}.txt', 'add', b'x') for i in range(publish.MAX_CHANGES + 1)])
        with self.assertRaises(publish.PublishError):
            self.check([('big.bin', 'add', b'x' * (publish.MAX_FILE_BYTES + 1))])

    def test_schema_rejects_wrong_types_unknown_keys_and_old_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, root = build_artifact(tmp, [('a.txt', 'add', b'x')])
            def mutate(fn):
                bad = json.loads(json.dumps(good))
                fn(bad)
                with self.assertRaises(publish.PublishError):
                    publish.validate_manifest(bad, root)
            mutate(lambda m: m.update(target={'pr': 1, 'branch': 'main'}))      # forged target identity
            mutate(lambda m: m.pop('unsupported'))
            mutate(lambda m: m.update(version=1))
            mutate(lambda m: m.update(version='2'))
            mutate(lambda m: m.update(base_sha='A' * 40))
            mutate(lambda m: m.update(base_sha=None))
            mutate(lambda m: m.update(changes='not-a-list'))
            mutate(lambda m: m.update(changes=['x']))
            mutate(lambda m: m['changes'][0].update(extra='x'))
            mutate(lambda m: m['changes'][0].update(size=True))
            mutate(lambda m: m['changes'][0].update(size='1'))
            mutate(lambda m: m['changes'][0].update(size=-1))
            mutate(lambda m: m['changes'][0].update(sha256='XYZ'))
            mutate(lambda m: m['changes'][0].update(sha256=123))
            mutate(lambda m: m['changes'][0].update(op='rename'))
            mutate(lambda m: m['changes'][0].update(path=['a']))
            mutate(lambda m: m['changes'][0].update(blob=5))
            mutate(lambda m: m['changes'].append(dict(m['changes'][0])))           # duplicate path/blob
            for junk in (None, [], 'x', 5):
                with self.assertRaises(publish.PublishError):
                    publish.validate_manifest(junk, root)

    def test_undeclared_artifact_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = build_artifact(tmp, [('a.txt', 'add', b'x')])
            Path(root, 'files/99').write_bytes(b'smuggled')
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = build_artifact(tmp, [('a.txt', 'add', b'x')])
            Path(root, 'extra.sh').write_text('x')
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)

    def test_load_manifest_handles_malformed_files_safely(self):
        for content in ('{not json', '[]', '"x"', 'null', ''):
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'manifest.json').write_text(content)
                with self.assertRaises(publish.PublishError, msg=content):
                    publish.load_manifest(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(publish.PublishError):
                publish.load_manifest(tmp)


class CodexReproductionTests(unittest.TestCase):
    """The three publisher failures from the first independent review must not publish."""

    def run_publish(self, api, env=ENV, plan=PLAN):
        return publish.publish(api, env, plan, sleep=lambda _s: None, git_push=api.push)

    def test_agent_failure_cannot_publish(self):
        for status in ('failure', 'failed', 'cancelled', 'timeout', '', 'Success'):
            api = FakeApi()
            with self.assertRaises(publish.PublishError, msg=status):
                self.run_publish(api, env={**ENV, 'GENERATION_STATUS': status})
            self.assertEqual(api.calls, [], status)  # not even a read

    def test_unrelated_branch_and_mismatched_pr_number_cannot_publish(self):
        base_pr = FakeApi().pr
        cases = {
            'pr number differs': FakeApi(pr={**base_pr, 'number': 99}),
            'unrelated head branch': FakeApi(pr={**base_pr, 'head': {
                'ref': 'someone/else', 'sha': BASE, 'repo': {'full_name': 'AhmedMahmoud2222/DCIM'}}}),
            'fork head': FakeApi(pr={**base_pr, 'head': {
                'ref': 'feature/x', 'sha': BASE, 'repo': {'full_name': 'evil/DCIM'}}}),
            'foreign base repo': FakeApi(pr={**base_pr, 'base': {'repo': {'full_name': 'evil/DCIM'}}}),
            'closed pr': FakeApi(pr={**base_pr, 'state': 'closed'}),
            'merged pr': FakeApi(pr={**base_pr, 'merged': True}),
            'head moved': FakeApi(pr={**base_pr, 'head': {
                'ref': 'feature/x', 'sha': MOVED, 'repo': {'full_name': 'AhmedMahmoud2222/DCIM'}}}),
            'branch tip moved': FakeApi(branch_tip=MOVED),
            'protected branch': FakeApi(protected=True),
        }
        for name, api in cases.items():
            with self.assertRaises(publish.PublishError, msg=name):
                self.run_publish(api, env=PR_ENV)
            self.assertEqual(api.mutations(), [], name)

    def test_artifact_cannot_redirect_the_target(self):
        api = FakeApi(pr={**FakeApi().pr, 'number': 3, 'head': {
            'ref': 'unrelated', 'sha': BASE, 'repo': {'full_name': 'AhmedMahmoud2222/DCIM'}}})
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env={**PR_ENV, 'TARGET_NUMBER': '3'})
        self.assertEqual(api.mutations(), [])

    def test_forged_artifact_baseline_cannot_publish(self):
        for forged in (MOVED, 'd' * 40):
            for env in (ENV, PR_ENV):
                api = FakeApi()
                with self.assertRaises(publish.PublishError):
                    self.run_publish(api, env=env, plan={**PLAN, 'base_sha': forged})
                self.assertEqual(api.mutations(), [])

    def test_three_way_agreement_required_for_pr_updates(self):
        self.assertTrue(self.run_publish(FakeApi(), env=PR_ENV)['published'])      # all three agree
        with self.assertRaises(publish.PublishError):                              # trusted differs
            self.run_publish(FakeApi(), env={**PR_ENV, 'AUTHORIZED_SHA': MOVED})
        api = FakeApi(branch_tip=MOVED)                                            # live differs
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env=PR_ENV)
        self.assertEqual(api.mutations(), [])

    def test_non_sha_trusted_baseline_rejected(self):
        api = FakeApi()
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env={**ENV, 'AUTHORIZED_SHA': 'HEAD'}, plan={**PLAN, 'base_sha': 'HEAD'})
        self.assertEqual(api.mutations(), [])


class PublishFlowTests(unittest.TestCase):
    def run_publish(self, api, env=ENV, plan=PLAN, evidence=None):
        return publish.publish(api, env, plan, sleep=lambda _s: None, git_push=api.push,
                               evidence=evidence if evidence is not None else {})

    def test_new_branch_flow_creates_signed_commit_and_draft_pr(self):
        api = FakeApi()
        result = self.run_publish(api)
        self.assertEqual(result['branch'], 'opencode/issue7-1-1')
        self.assertTrue(result['verified'])
        pr_call = next(c for c in api.calls if c[1] == 'pulls' and c[0] == 'POST')
        self.assertIs(pr_call[2]['draft'], True)
        self.assertEqual(pr_call[2]['base'], 'main')
        self.assertIn('Refs #7', pr_call[2]['body'])
        self.assertNotIn('Closes', pr_call[2]['body'])
        graphql = next(c for c in api.calls if c[0] == 'GRAPHQL')[2]['input']
        self.assertEqual(graphql['expectedHeadOid'], BASE)
        self.assertNotIn('\n', graphql['message']['headline'])
        self.assertIn('OpenCode-Model: opencode/deepseek-v4.1-flash-free', graphql['message']['body'])
        self.assertNotIn(('DELETE', 'git/refs/heads/opencode/issue7-1-1'), api.methods())

    def test_new_branch_requires_open_non_pr_issue_and_reachable_base(self):
        for api in (FakeApi(issue={'number': 7, 'state': 'closed'}),
                    FakeApi(issue={'number': 7, 'state': 'open', 'pull_request': {}}),
                    FakeApi(issue={'number': 8, 'state': 'open'}),
                    FakeApi(compare='diverged'), FakeApi(default_protected=False)):
            with self.assertRaises(publish.PublishError):
                self.run_publish(api)
            self.assertEqual(api.mutations(), [])

    def test_new_branch_unverified_commit_deletes_branch_and_opens_no_pr(self):
        api = FakeApi(verified=False)
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api)
        self.assertIn('not GitHub-verified', str(caught.exception))
        self.assertIn(('DELETE', 'git/refs/heads/opencode/issue7-1-1'), api.methods())
        self.assertNotIn(('POST', 'pulls'), api.methods())
        self.assertNotIn('opencode/issue7-1-1', api.refs)

    def test_new_branch_unverified_and_undeletable_is_reported_as_partial_with_the_observed_state(self):
        api = FakeApi(verified=False, delete_fails=True)
        with self.assertRaises(publish.PartialPublication) as caught:
            self.run_publish(api)
        self.assertEqual(caught.exception.branch, 'opencode/issue7-1-1')
        self.assertEqual(caught.exception.commit, NEW)
        self.assertIn(f'still exists at {NEW}', str(caught.exception))

    def test_new_branch_pr_creation_failure_is_reported_as_partial(self):
        api = FakeApi(pulls_fails=True)
        with self.assertRaises(publish.PartialPublication) as caught:
            self.run_publish(api)
        self.assertIn('Draft PR could not be opened', str(caught.exception))

    def test_existing_branch_commits_on_staging_verifies_then_updates_exactly(self):
        api = FakeApi()
        result = self.run_publish(api, env=PR_ENV)
        self.assertEqual((result['branch'], result['pr']), ('feature/x', None))
        order = api.methods()
        staging = 'opencode/pr9-1-1'
        graphql = next(c for c in api.calls if c[0] == 'GRAPHQL')[2]['input']
        self.assertEqual(graphql['branch']['branchName'], staging)   # never committed on the PR branch directly
        verify_index = order.index(('GET', f'commits/{NEW}'))
        push_index = order.index(('PUSH', f'feature/x {BASE[:7]}->{NEW[:7]}'))
        self.assertLess(verify_index, push_index)
        self.assertEqual(api.refs['feature/x'], NEW)
        self.assertNotIn(staging, api.refs)
        self.assertNotIn(('POST', 'pulls'), order)
        self.assertFalse([m for m, _ in order if m == 'PATCH'])       # the non-CAS REST update is never used

    def test_the_update_requires_the_verified_commit_to_descend_from_the_authorized_baseline(self):
        for descent in ({'status': 'diverged', 'behind_by': 1}, {'status': 'ahead', 'behind_by': 2},
                        {'status': 'behind', 'behind_by': 0}, {'status': 'identical', 'behind_by': 0}, {}):
            api = FakeApi(descent=descent or {'status': None})
            with self.assertRaises(publish.PublishError) as caught:
                self.run_publish(api, env=PR_ENV)
            self.assertIn('does not descend from the authorized baseline', str(caught.exception), descent)
            self.assertFalse([c for c in api.calls if c[0] == 'PUSH'], f'{descent}: the branch was pushed')
            self.assertEqual(api.refs['feature/x'], BASE, descent)

    def test_final_authorization_refetches_the_pr_immediately_before_the_update(self):
        api = FakeApi()
        self.run_publish(api, env=PR_ENV)
        order = api.methods()
        push_index = next(i for i, c in enumerate(order) if c[0] == 'PUSH')
        verify_index = order.index(('GET', f'commits/{NEW}'))
        refetches = [i for i, c in enumerate(order) if c == ('GET', 'pulls/9') and verify_index < i < push_index]
        self.assertTrue(refetches, 'PR was not refetched between verification and the update')
        self.assertTrue(any(c == ('GET', 'branches/feature/x') and verify_index < i < push_index
                            for i, c in enumerate(order)))

    def test_pr_closed_merged_or_retargeted_after_verification_aborts_before_the_update(self):
        for state in ('closed',):
            api = FakeApi()
            api.pr_state_after_verify = state
            with self.assertRaises(publish.PublishError) as caught:
                self.run_publish(api, env=PR_ENV)
            self.assertIn('not open', str(caught.exception))
            self.assertFalse([c for c in api.calls if c[0] == 'PUSH'])
            self.assertEqual(api.refs['feature/x'], BASE)
            self.assertNotIn('opencode/pr9-1-1', api.refs)
        api = FakeApi()
        api.before_push = None
        original = api.call

        def merged(method, path, body=None):
            result = original(method, path, body)
            if path == 'pulls/9' and any(c[1].startswith('commits/') for c in api.calls):
                result = {**result, 'merged': True}
            return result
        api.call = merged
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env=PR_ENV)
        self.assertFalse([c for c in api.calls if c[0] == 'PUSH'])

    def test_existing_branch_verification_failure_leaves_pr_branch_untouched(self):
        api = FakeApi(verified=False)
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env=PR_ENV)
        self.assertFalse([c for c in api.calls if c[0] == 'PUSH'])
        self.assertEqual(api.refs['feature/x'], BASE)
        self.assertNotIn('opencode/pr9-1-1', api.refs)

    def test_existing_branch_commit_by_wrong_identity_is_never_applied(self):
        api = FakeApi(login='AhmedMahmoud2222')
        with self.assertRaises(publish.PublishError):
            self.run_publish(api, env=PR_ENV)
        self.assertFalse([c for c in api.calls if c[0] == 'PUSH'])

    def test_concurrent_move_makes_the_update_fail_and_the_report_is_verified_not_assumed(self):
        api = FakeApi()
        api.before_push = lambda: api.refs.__setitem__('feature/x', MOVED)   # someone pushes during the window
        evidence = {}
        with self.assertRaises(publish.PartialPublication) as caught:
            self.run_publish(api, env=PR_ENV, evidence=evidence)
        self.assertEqual(api.refs['feature/x'], MOVED)                        # their change survives
        self.assertEqual(evidence['pr_branch_observed_after_failure'], MOVED)
        self.assertIn('could not be confirmed as unchanged', str(caught.exception))
        self.assertNotIn('opencode/pr9-1-1', api.refs)

    def test_failed_update_with_branch_verified_unchanged_says_so(self):
        api = FakeApi(push_fails=True)
        evidence = {}
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api, env=PR_ENV, evidence=evidence)
        self.assertNotIsInstance(caught.exception, publish.PartialPublication)
        self.assertIn(f'is still at the authorized commit {BASE}', str(caught.exception))
        self.assertEqual(evidence['pr_branch_observed_after_failure'], BASE)

    def test_cleanup_failure_after_success_is_a_warning_not_a_rewrite(self):
        api = FakeApi(delete_fails=True)
        result = self.run_publish(api, env=PR_ENV)
        self.assertTrue(result['published'])
        self.assertTrue(any('still exists' in w for w in result['warnings']))

    # ---- ambiguous GitHub failures: query the real remote state before claiming anything
    def test_ambiguous_ref_creation_that_was_applied_continues(self):
        api = FakeApi(ambiguous={'POST git/refs': 'applied'})
        self.assertTrue(self.run_publish(api)['published'])

    def test_ambiguous_ref_creation_that_was_not_applied_stops_with_the_observed_state(self):
        api = FakeApi(ambiguous={'POST git/refs': 'not_applied'})
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api)
        self.assertIn('observed remote state: None', str(caught.exception))

    def test_ambiguous_commit_that_landed_is_found_verified_and_published(self):
        api = FakeApi(ambiguous={'GRAPHQL createCommitOnBranch': 'applied'})
        result = self.run_publish(api)
        self.assertEqual(result['commit'], NEW)
        self.assertTrue(result['verified'])

    def test_ambiguous_commit_that_did_not_land_is_reported_from_observation(self):
        api = FakeApi(ambiguous={'GRAPHQL createCommitOnBranch': 'not_applied'})
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api)
        self.assertIn(f'verified: opencode/issue7-1-1 is at {BASE}', str(caught.exception))
        self.assertNotIn('opencode/issue7-1-1', api.refs)    # staging branch cleaned up

    def test_ambiguous_update_that_actually_applied_is_reported_as_applied(self):
        api = FakeApi(ambiguous={'PUSH feature/x': 'applied'})
        evidence = {}
        result = self.run_publish(api, env=PR_ENV, evidence=evidence)
        self.assertEqual(api.refs['feature/x'], NEW)
        self.assertTrue(any('is at' in w for w in result['warnings']))
        self.assertEqual(evidence['pr_branch_observed_after_failure'], NEW)

    def test_ambiguous_update_that_did_not_apply_is_reported_as_unchanged_only_after_checking(self):
        api = FakeApi(ambiguous={'PUSH feature/x': 'not_applied'})
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api, env=PR_ENV)
        self.assertIn('verified: feature/x is still at the authorized commit', str(caught.exception))

    def test_unknowable_remote_state_is_never_reported_as_unchanged(self):
        api = FakeApi(push_fails=True)
        original = api.call

        def flaky(method, path, body=None):
            if path.startswith('git/ref/heads/feature/x'):
                raise publish.AmbiguousApiError('unreachable')
            return original(method, path, body)
        api.call = flaky
        evidence = {}
        with self.assertRaises(publish.PartialPublication) as caught:
            self.run_publish(api, env=PR_ENV, evidence=evidence)
        self.assertNotIn('still at the authorized commit', str(caught.exception))
        self.assertIn('could not be confirmed as unchanged', str(caught.exception))
        self.assertIn('pr_branch_lookup_error', evidence)

    def test_ambiguous_delete_is_confirmed_by_lookup(self):
        api = FakeApi(verified=False, ambiguous={'DELETE git/refs/heads/opencode/*': 'applied'})
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api)
        self.assertNotIsInstance(caught.exception, publish.PartialPublication)   # branch really is gone
        api = FakeApi(verified=False, ambiguous={'DELETE git/refs/heads/opencode/*': 'not_applied'})
        with self.assertRaises(publish.PartialPublication):                      # it is still there
            self.run_publish(api)

    def test_evidence_is_written_for_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp, 'a')
            artifact.mkdir()
            manifest, _ = build_artifact(artifact, [('a.txt', 'add', b'x')])
            Path(artifact, 'manifest.json').write_text(json.dumps(manifest))
            evidence_path = Path(tmp, 'evidence.json')
            old = dict(os.environ)
            original = publish.Api
            try:
                os.environ.clear()
                os.environ.update({**ENV, 'ARTIFACT_DIR': str(artifact), 'GH_PUBLISH_TOKEN': 'tok', 'EVIDENCE_PATH': str(evidence_path),
                                   'GITHUB_OUTPUT': str(Path(tmp, 'out')), 'EVENT_KIND': 'pr', 'TARGET_NUMBER': '9',
                                   'PR_HEAD_REF': 'feature/x'})
                api = FakeApi(push_fails=True)
                publish.Api = lambda token, repo: api
                os.environ['GIT_REPO_DIR'] = tmp
                original_publish = publish.publish
                publish.publish = lambda api_, env_, plan_, sleep=None, git_push=None, evidence=None: original_publish(
                    api_, env_, plan_, sleep=lambda _s: None, git_push=api.push, evidence=evidence)
                self.assertEqual(publish.main(), 1)
                data = json.loads(evidence_path.read_text())
                self.assertEqual(data['trusted_baseline'], BASE)
                self.assertIn('failure', data)
                self.assertEqual(data['pr_branch_observed_after_failure'], BASE)
            finally:
                publish.Api = original
                publish.publish = original_publish
                os.environ.clear()
                os.environ.update(old)

    def test_never_writes_to_default_or_protected_branch(self):
        with self.assertRaises(publish.PublishError):
            self.run_publish(FakeApi(), env={**PR_ENV, 'PR_HEAD_REF': 'main'})
        with self.assertRaises(publish.PublishError):
            publish.check_branch('issue', 'feature/x', 'main', False)
        with self.assertRaises(publish.PublishError):
            publish.check_branch('pr', 'release/1', 'main', True)

    def test_empty_plan_publishes_nothing(self):
        api = FakeApi()
        result = publish.publish(api, ENV, {'base_sha': BASE, 'additions': [], 'deletions': []})
        self.assertFalse(result['published'])
        self.assertEqual(api.calls, [])

    def test_missing_credentials_fail_with_actionable_message_and_no_secret(self):
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update({'ARTIFACT_DIR': '/nonexistent'})
            self.assertEqual(publish.main(), 1)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_main_reports_partial_publication_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'a').mkdir()
            artifact, _root = build_artifact(Path(tmp, 'a'), [('a.txt', 'add', b'x')])
            Path(tmp, 'a', 'manifest.json').write_text(json.dumps(artifact))
            out = Path(tmp, 'out')
            old = dict(os.environ)
            original = publish.publish
            try:
                os.environ.clear()
                os.environ.update({**ENV, 'ARTIFACT_DIR': str(Path(tmp, 'a')), 'GH_PUBLISH_TOKEN': 'x',
                                   'GITHUB_OUTPUT': str(out)})
                publish.publish = lambda *a, **k: (_ for _ in ()).throw(
                    publish.PartialPublication('x', 'opencode/issue7-1-1', NEW))
                self.assertEqual(publish.main(), 1)
                text = out.read_text()
                self.assertIn('published=partial', text)
                self.assertIn('branch=opencode/issue7-1-1', text)
            finally:
                publish.publish = original
                os.environ.clear()
                os.environ.update(old)

    def test_http_status_classification_decides_whether_an_outcome_is_ambiguous(self):
        import urllib.error
        api = publish.Api('ghs_TOKEN', 'o/r')
        for code in (500, 502, 503, 504):
            error = urllib.error.HTTPError('https://api.github.com/x', code, 'server error', {}, None)
            with unittest.mock.patch.object(publish.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(publish.AmbiguousApiError, msg=str(code)) as caught:
                    api.call('PATCH', 'git/refs/heads/x', {'sha': NEW})
            self.assertEqual(caught.exception.status, code)
        for code in (400, 401, 403, 404, 409, 422):
            error = urllib.error.HTTPError('https://api.github.com/x', code, 'client error', {}, None)
            with unittest.mock.patch.object(publish.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(publish.PublishError, msg=str(code)) as caught:
                    api.call('PATCH', 'git/refs/heads/x', {'sha': NEW})
            self.assertNotIsInstance(caught.exception, publish.AmbiguousApiError, code)   # a definite answer, not a guess

    def test_api_errors_do_not_leak_tokens_and_classify_ambiguity(self):
        api = publish.Api('ghs_SECRETTOKENVALUE', 'o/r')
        with self.assertRaises(publish.AmbiguousApiError) as caught:
            api.call('GET', 'https://127.0.0.1:1/never')
        self.assertNotIn('SECRETTOKENVALUE', str(caught.exception))


class CollectorTests(unittest.TestCase):
    def git(self, repo, *args):
        subprocess.run(['git', '-C', repo, '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True)

    def make_repo(self, tmp):
        repo = os.path.join(tmp, 'repo')
        os.makedirs(repo)
        self.git(repo, 'init', '-q')
        self.git(repo, 'config', 'user.email', 't@example.com')
        self.git(repo, 'config', 'user.name', 't')
        for name, body in (('keep.txt', 'one\n'), ('gone.txt', 'bye\n'), ('.env', 'A=1\n'), ('.gitignore', '*.log\n')):
            Path(repo, name).write_text(body)
        os.makedirs(os.path.join(repo, '.opencode'))
        Path(repo, '.opencode', 'cmd.md').write_text('x')
        self.git(repo, 'add', '-A', '-f')
        self.git(repo, 'commit', '-qm', 'base')
        return repo, subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                    check=True).stdout.strip()

    def test_collects_changes_against_sanitized_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self.make_repo(tmp)
            work, state, out = (os.path.join(tmp, n) for n in ('work', 'state', 'out'))
            meta = sandbox.prepare(repo, work, state, sha, set())
            excluded = {item['path'] for item in meta['excluded']}
            self.assertIn('.env', excluded)
            self.assertIn('.opencode/cmd.md', excluded)
            self.assertFalse(os.path.exists(os.path.join(work, '.env')))
            Path(work, 'keep.txt').write_text('two\n')
            Path(work, 'new.txt').write_text('new\n')
            Path(work, 'gone.txt').unlink()
            os.symlink('keep.txt', os.path.join(work, 'link'))
            Path(work, 'ignored.log').write_text('x')
            manifest = collect.collect(work, state, out)
            ops = {c['path']: c['op'] for c in manifest['changes']}
            self.assertEqual(ops, {'keep.txt': 'modify', 'new.txt': 'add', 'gone.txt': 'delete'})
            self.assertEqual(manifest['base_sha'], sha)  # the trusted authorized SHA, not a derived value
            self.assertEqual([u['path'] for u in manifest['unsupported']], ['link'])
            with self.assertRaises(publish.PublishError):  # symlink blocks publication
                publish.load_manifest(out)

    def test_excluded_files_are_not_reported_as_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self.make_repo(tmp)
            work, state, out = (os.path.join(tmp, n) for n in ('work', 'state', 'out'))
            sandbox.prepare(repo, work, state, sha, set())
            manifest = collect.collect(work, state, out)
            self.assertEqual(manifest['changes'], [])

    def test_agent_planted_git_config_is_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self.make_repo(tmp)
            work, state, out = (os.path.join(tmp, n) for n in ('work', 'state', 'out'))
            sandbox.prepare(repo, work, state, sha, set())
            marker = os.path.join(tmp, 'pwned')
            hook = os.path.join(tmp, 'evil.sh')
            Path(hook).write_text(f'#!/bin/sh\ntouch {marker}\n')
            os.chmod(hook, 0o755)
            os.makedirs(os.path.join(work, '.git', 'hooks'))
            Path(work, '.git', 'config').write_text(f'[core]\n\tfsmonitor = {hook}\n\thooksPath = {tmp}\n'
                                                     f'[diff]\n\texternal = {hook}\n')
            Path(work, '.gitattributes').write_text('* filter=evil diff=evil\n')
            Path(work, 'keep.txt').write_text('two\n')
            collect.collect(work, state, out)
            self.assertFalse(os.path.exists(marker))


class SignatureVerifierTests(unittest.TestCase):
    def test_range_mode_reports_unverified_commits(self):
        good = {'sha': 'a' * 40, 'commit': {'verification': {'verified': True, 'reason': 'valid'}}}
        bad = {'sha': 'b' * 40, 'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}}
        self.assertEqual(verify.unverified([good]), [])
        self.assertEqual(verify.unverified([good, bad]), ['bbbbbbbbbbbb (unsigned)'])
        self.assertEqual(len(verify.unverified([{'sha': 'c' * 40}])), 1)

    def test_single_commit_mode_requires_valid_signature_and_app_author(self):
        ok = {'commit': {'verification': {'verified': True, 'reason': 'valid'}}, 'author': {'login': BOT}}
        self.assertEqual(verify.check_commit(ok, BOT), '')
        self.assertIn('not verified', verify.check_commit(
            {'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}, 'author': {'login': BOT}}, BOT))
        self.assertIn('not verified', verify.check_commit(
            {'commit': {'verification': {'verified': True, 'reason': 'unknown_key'}}, 'author': {'login': BOT}}, BOT))
        self.assertIn('unexpected author', verify.check_commit(
            {'commit': {'verification': {'verified': True, 'reason': 'valid'}}, 'author': {'login': 'someone'}}, BOT))


if __name__ == '__main__':
    unittest.main()
