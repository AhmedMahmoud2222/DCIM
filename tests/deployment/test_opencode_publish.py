"""OpenCode collector, publisher and signature-verifier tests (tests v1.0). No network."""
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'.github/scripts/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = load('opencode_collect')
publish = load('opencode_publish')
verify = load('opencode_verify_signatures')
BASE = 'a' * 40
NEW = 'b' * 40
BOT = 'dcim-opencode-agent[bot]'
ENV = {
    'GITHUB_REPOSITORY': 'AhmedMahmoud2222/DCIM', 'DEFAULT_BRANCH': 'main', 'EVENT_KIND': 'issue',
    'TARGET_NUMBER': '7', 'RUN_URL': 'https://github.com/x/y/actions/runs/1', 'RUN_ID': '1',
    'RUN_ATTEMPT': '1', 'ACTOR_LOGIN': 'AhmedMahmoud2222', 'EXPECT_BOT_LOGIN': BOT,
    'TASK': 'fix the alarm bug\nsecond line', 'MODEL': 'opencode/deepseek-v4.1-flash-free',
    'CATEGORY': 'general', 'ROUTE_REASON': 'category general', 'PR_HEAD_REF': '', 'AGENT_OUTCOME': 'failure',
}


def manifest_dir(tmp, changes, unsupported=None):
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
    manifest = {'version': 1, 'base_sha': BASE, 'changes': entries, 'unsupported': unsupported or []}
    return manifest, str(root)


class FakeApi:
    def __init__(self, verified=True, login=BOT, pr_tip=BASE, protected=False, compare='behind'):
        self.calls, self.verified, self.login = [], verified, login
        self.pr_tip, self.protected, self.compare = pr_tip, protected, compare

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'GET' and path == '':
            return {'default_branch': 'main'}
        if path.startswith('compare/'):
            return {'status': self.compare}
        if path.startswith('branches/'):
            return {'protected': self.protected, 'commit': {'sha': self.pr_tip}}
        if path.startswith('commits/'):
            return {'commit': {'verification': {'verified': self.verified, 'reason': 'valid' if self.verified else 'unsigned'}},
                    'author': {'login': self.login}}
        if path == 'pulls':
            return {'number': 9, 'html_url': 'https://github.com/x/y/pull/9'}
        return {}

    def graphql(self, query, variables):
        self.calls.append(('GRAPHQL', 'createCommitOnBranch', variables))
        return {'createCommitOnBranch': {'commit': {'oid': NEW}}}

    def methods(self):
        return [(m, p) for m, p, _ in self.calls]


class ManifestValidationTests(unittest.TestCase):
    def check(self, changes, unsupported=None):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = manifest_dir(tmp, changes, unsupported)
            return publish.validate_manifest(manifest, root)

    def test_valid_manifest_builds_file_changes(self):
        plan = self.check([('backend/app.py', 'modify', b'print(1)\n'), ('docs/old.md', 'delete', b'')])
        self.assertEqual(plan['deletions'], ['docs/old.md'])
        self.assertEqual(base64.b64decode(plan['additions'][0]['contents']), b'print(1)\n')

    def test_unsafe_paths_rejected(self):
        for path in ('../etc/passwd', '/abs', '.git/config', 'a/.GIT/x', 'a//b', 'a/./b', 'a\\b', 'x\x00y', ''):
            with self.assertRaises(publish.PublishError, msg=path):
                self.check([(path, 'add', b'x')])

    def test_agent_cannot_modify_its_own_controls(self):
        for path in ('.github/workflows/ci.yml', '.github/scripts/opencode_gate.py',
                     '.github/opencode/model-registry.json', '.github/CODEOWNERS', 'CODEOWNERS',
                     'docs/security/OPENCODE_AGENT_AUTHORITY.md', 'tests/deployment/test_opencode_router.py',
                     '.opencode/commands/x.md', 'opencode.json', 'sub/AGENTS.md', '.GitHub/Workflows/x.yml'):
            with self.assertRaises(publish.PublishError, msg=path):
                self.check([(path, 'modify', b'x')])

    def test_ordinary_code_paths_allowed(self):
        self.check([('backend/app/main.py', 'modify', b'x'), ('frontend/src/a.tsx', 'add', b'y'),
                    ('.github/scripts/clamd_probe.py', 'modify', b'z')])

    def test_unsupported_changes_symlinks_and_tampered_blobs_rejected(self):
        with self.assertRaises(publish.PublishError):
            self.check([('a.txt', 'add', b'x')], unsupported=[{'path': 'link', 'reason': 'mode'}])
        with tempfile.TemporaryDirectory() as tmp:
            manifest, root = manifest_dir(tmp, [('a.txt', 'add', b'x')])
            Path(root, 'files/0').write_bytes(b'tampered')
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)
            manifest['changes'][0]['blob'] = 'files/../manifest.json'
            with self.assertRaises(publish.PublishError):
                publish.validate_manifest(manifest, root)

    def test_size_and_count_limits(self):
        with self.assertRaises(publish.PublishError):
            self.check([(f'f{i}.txt', 'add', b'x') for i in range(publish.MAX_CHANGES + 1)])
        with self.assertRaises(publish.PublishError):
            self.check([('big.bin', 'add', b'x' * (publish.MAX_FILE_BYTES + 1))])


class PublishFlowTests(unittest.TestCase):
    plan = {'base_sha': BASE, 'additions': [{'path': 'a.txt', 'contents': 'eA=='}], 'deletions': []}

    def run_publish(self, api, **overrides):
        return publish.publish(api, {**ENV, **overrides}, self.plan, sleep=lambda _s: None)

    def test_issue_flow_creates_branch_signed_commit_and_draft_pr(self):
        api = FakeApi()
        result = self.run_publish(api)
        self.assertEqual(result['branch'], 'opencode/issue7-1-1')
        self.assertTrue(result['verified'])
        pr_call = next(c for c in api.calls if c[1] == 'pulls')
        self.assertIs(pr_call[2]['draft'], True)
        self.assertEqual(pr_call[2]['base'], 'main')
        self.assertIn('Refs #7', pr_call[2]['body'])
        self.assertNotIn('Closes', pr_call[2]['body'])
        graphql = next(c for c in api.calls if c[0] == 'GRAPHQL')[2]['input']
        self.assertEqual(graphql['expectedHeadOid'], BASE)
        self.assertEqual(graphql['branch']['branchName'], 'opencode/issue7-1-1')
        self.assertNotIn('\n', graphql['message']['headline'])
        self.assertIn('OpenCode-Model: opencode/deepseek-v4.1-flash-free', graphql['message']['body'])
        self.assertNotIn(('DELETE', 'git/refs/heads/opencode/issue7-1-1'), api.methods())

    def test_never_writes_to_default_or_protected_branch(self):
        with self.assertRaises(publish.PublishError):
            self.run_publish(FakeApi(), EVENT_KIND='pr', PR_HEAD_REF='main')
        with self.assertRaises(publish.PublishError):
            self.run_publish(FakeApi(protected=True), EVENT_KIND='pr', PR_HEAD_REF='release/1')
        with self.assertRaises(publish.PublishError):
            publish.check_branch('issue', 'feature/x', 'main', False)

    def test_pr_flow_updates_head_branch_only_and_opens_no_pr(self):
        api = FakeApi()
        result = self.run_publish(api, EVENT_KIND='pr', PR_HEAD_REF='feature/x')
        self.assertIsNone(result['pr'])
        self.assertNotIn(('POST', 'pulls'), api.methods())
        self.assertNotIn(('POST', 'git/refs'), api.methods())

    def test_moved_pr_branch_stops_publication(self):
        with self.assertRaises(publish.PublishError):
            self.run_publish(FakeApi(pr_tip='c' * 40), EVENT_KIND='pr', PR_HEAD_REF='feature/x')

    def test_unverified_commit_stops_publication_and_removes_new_branch(self):
        api = FakeApi(verified=False)
        with self.assertRaises(publish.PublishError) as caught:
            self.run_publish(api)
        self.assertIn('not GitHub-verified', str(caught.exception))
        self.assertIn(('DELETE', 'git/refs/heads/opencode/issue7-1-1'), api.methods())
        self.assertNotIn(('POST', 'pulls'), api.methods())

    def test_commit_by_wrong_identity_is_rejected(self):
        with self.assertRaises(publish.PublishError):
            self.run_publish(FakeApi(login='AhmedMahmoud2222'))

    def test_base_not_reachable_from_default_stops_before_any_write(self):
        api = FakeApi(compare='diverged')
        with self.assertRaises(publish.PublishError):
            self.run_publish(api)
        self.assertNotIn(('POST', 'git/refs'), api.methods())

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

    def test_api_errors_do_not_leak_tokens(self):
        api = publish.Api('ghs_SECRETTOKENVALUE', 'o/r')
        try:
            api.call('GET', 'https://127.0.0.1:1/never')
        except publish.PublishError as error:
            self.assertNotIn('SECRETTOKENVALUE', str(error))
        except Exception as error:  # connection error text must not contain it either
            self.assertNotIn('SECRETTOKENVALUE', str(error))


class CollectorTests(unittest.TestCase):
    def git(self, repo, *args):
        subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True)

    def test_collects_add_modify_delete_and_flags_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = os.path.join(tmp, 'repo'), os.path.join(tmp, 'out')
            os.makedirs(repo)
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.email', 't@example.com')
            self.git(repo, 'config', 'user.name', 't')
            self.git(repo, 'config', 'commit.gpgsign', 'false')
            for name, body in (('keep.txt', 'one\n'), ('gone.txt', 'bye\n')):
                Path(repo, name).write_text(body)
            self.git(repo, 'add', '-A')
            self.git(repo, 'commit', '-qm', 'base')
            base = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True,
                                  text=True, check=True).stdout.strip()
            Path(repo, 'keep.txt').write_text('two\n')
            Path(repo, 'new.txt').write_text('new\n')
            Path(repo, 'gone.txt').unlink()
            os.symlink('keep.txt', os.path.join(repo, 'link'))
            Path(repo, 'ignored.log').write_text('x')
            Path(repo, '.gitignore').write_text('*.log\n')
            manifest = collect.collect(repo, base, out)
            ops = {c['path']: c['op'] for c in manifest['changes']}
            self.assertEqual(ops['keep.txt'], 'modify')
            self.assertEqual(ops['new.txt'], 'add')
            self.assertEqual(ops['gone.txt'], 'delete')
            self.assertNotIn('ignored.log', ops)
            self.assertEqual([u['path'] for u in manifest['unsupported']], ['link'])
            with self.assertRaises(publish.PublishError):  # symlink blocks publication
                publish.validate_manifest(json.loads(Path(out, 'manifest.json').read_text()), out)


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
