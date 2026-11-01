"""Adversarial regression tests for the Claude agent's read-only / publisher split (tests v2.0).

P1: executable agent configuration (.claude/, .mcp.json, bunfig.toml ...) must never come from the PR.
    Only the protected default branch may be a base, the PR checkout lives outside the workspace root
    the action and CLI read, and executable configuration inside it is removed.
P2: the Claude job holds a read-only token. Only the publisher writes, after re-validating a hostile
    manifest, and only by a non-forced fast-forward of the existing PR branch. A PR that closes, merges,
    moves HEAD or loses its branch can therefore never cause a branch to be created or overwritten.
"""
import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPT = ROOT / '.github/scripts/claude_publish.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('test_claude_agent_helpers', HERE / 'test_claude_agent.py')
claude, publisher = base.claude, base.publisher
event, pr, decide, load_workflow = base.event, base.pr, base.decide, base.load_workflow
REPO, SHA, BASE_SHA, PROTECTED_MAIN, OWNER_ID = base.REPO, base.SHA, base.BASE_SHA, base.PROTECTED_MAIN, base.OWNER_ID
OTHER, NEW_COMMIT, NEW_TREE, NEW_BLOB, BASE_TREE = 'd' * 40, '1' * 40, '2' * 40, '3' * 40, '4' * 40
TARGET = {'repository': REPO, 'number': 7, 'head_ref': 'feature/x', 'authorized_sha': SHA, 'comment_id': '99'}


def live_ref(sha=SHA, ref='feature/x'):
    return {'ref': f'refs/heads/{ref}', 'object': {'sha': sha}}


def b64(text):
    return base64.b64encode(text.encode()).decode()


def manifest(changes, **overrides):
    doc = {'authorized_sha': SHA, 'head_ref': 'feature/x', 'number': 7, 'changes': changes}
    doc.update(overrides)
    return doc


def upsert(path, text='x\n', mode='100644'):
    return {'op': 'upsert', 'path': path, 'mode': mode, 'content_b64': b64(text)}


class BaseTrustTests(unittest.TestCase):
    """P1: untrusted base-branch configuration must never reach a credentialed run."""

    def test_only_the_approved_protected_default_branch_is_a_valid_base(self):
        self.assertTrue(decide(event('@claude fix'))[0])
        for base_ref in ('develop', 'release/1', 'main-evil', 'refs/heads/main', 'Main', 'main ', '', None,
                         'attacker/hooks'):
            ok, reason = decide(event('@claude fix'), pull=pr(base_ref=base_ref))
            self.assertFalse(ok, base_ref)
            self.assertIn('approved', reason)

    def test_unprotected_missing_or_malformed_base_protection_rejected(self):
        for branch in ({'name': 'main', 'protected': False}, {'name': 'main'}, {'name': 'main', 'protected': 'true'},
                       {'name': 'main', 'protected': 1}, {'name': 'other', 'protected': True}, {}, None, [], 'x'):
            self.assertFalse(decide(event('@claude fix'), base_branch=branch)[0], branch)
        self.assertFalse(claude.decide('issue_comment', event('@claude fix'), REPO, {OWNER_ID}, pr())[0])

    def test_default_branch_must_still_be_the_approved_base(self):
        self.assertFalse(decide(event('@claude fix', default_branch='develop'))[0])
        self.assertFalse(decide(event('@claude fix', default_branch=None))[0])

    def test_approved_base_constants_agree(self):
        self.assertEqual(claude.APPROVED_BASE_REF, publisher.APPROVED_BASE_REF)
        self.assertEqual(claude.APPROVED_BASE_REF, 'main')

    def test_gate_fetches_live_base_protection_without_a_secret(self):
        gate = load_workflow()['jobs']['gate']
        self.assertIn('branches/main', (ROOT / '.github/workflows/claude-agent.yml').read_text())
        auth = next(s for s in gate['steps'] if s.get('id') == 'gate')
        self.assertIn('BASE_BRANCH_JSON_PATH', auth['env'])
        self.assertNotIn('secrets.', json.dumps(gate))

    def test_gate_output_cannot_be_forged_by_multiline_task_text(self):
        forged = 'line1\nauthorized_sha=' + 'e' * 40 + '\nhead_ref=main'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            claude.write_output(str(out), True, 'authorized', {'number': 7, 'authorized_sha': SHA, 'task': forged})
            parsed, lines, i = {}, out.read_text().splitlines(), 0
            while i < len(lines):  # the same rules the Actions runner applies to GITHUB_OUTPUT
                if '<<' in lines[i].split('=')[0]:
                    key, delimiter = lines[i].split('<<', 1)
                    j = i + 1
                    while lines[j] != delimiter:
                        j += 1
                    parsed[key] = '\n'.join(lines[i + 1:j])
                    i = j + 1
                else:
                    key, value = lines[i].split('=', 1)
                    parsed[key] = value
                    i += 1
        self.assertEqual(parsed['task'], forged)
        self.assertEqual(parsed['authorized_sha'], SHA)
        self.assertNotIn('head_ref', parsed)


class LiveCheckTests(unittest.TestCase):
    """The live-state check that authorizes the publisher."""

    def denied(self, pull=None, base_branch=PROTECTED_MAIN, ref_obj=None):
        with self.assertRaises(publisher.Denied):
            publisher.check_live(pr() if pull is None else pull, base_branch,
                                 live_ref() if ref_obj is None else ref_obj, TARGET)

    def test_unchanged_live_state_is_allowed(self):
        publisher.check_live(pr(), PROTECTED_MAIN, live_ref(), TARGET)

    def test_closed_or_merged_pr_denied(self):
        self.denied(pull=pr(state='closed'))
        self.denied(pull=pr(merged=True))
        self.denied(pull=pr(state='closed', merged=True))

    def test_head_change_denied(self):
        self.denied(pull=pr(sha=OTHER))
        self.denied(ref_obj=live_ref(sha=OTHER))

    def test_head_branch_deleted_or_renamed_denied_so_it_is_never_recreated(self):
        self.denied(ref_obj={})
        self.denied(ref_obj=live_ref(ref='claude/pr-7-1'))
        self.denied(pull=pr(ref='claude/pr-7-1'))

    def test_retargeted_or_unprotected_base_denied(self):
        self.denied(pull=pr(base_ref='attacker/hooks'))
        self.denied(base_branch={'name': 'main', 'protected': False})
        self.denied(base_branch=None)

    def test_fork_or_wrong_pr_denied(self):
        self.denied(pull=pr(head_repo='fork/DCIM'))
        self.denied(pull=pr(base_repo='fork/DCIM'))
        self.denied(pull=pr(number=8))
        self.denied(pull=[])


class ManifestValidationTests(unittest.TestCase):
    """The manifest is produced next to an agent that read untrusted text, so it is hostile input."""

    def denied(self, doc):
        with self.assertRaises(publisher.Denied):
            publisher.validate_manifest(doc, TARGET)

    def test_valid_manifest_is_accepted(self):
        out = publisher.validate_manifest(manifest([upsert('src/a.py'), {'op': 'delete', 'path': 'old.txt'}]), TARGET)
        self.assertEqual([c['path'] for c in out], ['src/a.py', 'old.txt'])

    def test_protected_and_executable_config_paths_rejected(self):
        for path in ('.github/workflows/claude-agent.yml', '.github/scripts/claude_gate.py', '.GitHub/workflows/x.yml',
                     '.claude/settings.json', 'sub/.claude/settings.json', '.mcp.json', '.claude.json',
                     '.gitmodules', '.ripgreprc', 'bunfig.toml', '.npmrc', '.husky/pre-commit', 'CLAUDE.md',
                     'docs/CLAUDE.md', 'claude.local.md', '.git/config', 'a/.git/hooks/x'):
            self.denied(manifest([upsert(path)]))
            self.denied(manifest([{'op': 'delete', 'path': path}]))

    def test_unsafe_paths_rejected(self):
        for path in ('/etc/passwd', '../x', 'a/../../x', 'a//b', './a', 'a/./b', 'a\\b', '', 'a\x00b', 'a\nb',
                     'x' * 501, None, 7, ['a']):
            self.denied(manifest([upsert(path) if isinstance(path, str) else {'op': 'upsert', 'path': path,
                                                                                'mode': '100644',
                                                                                'content_b64': b64('x')}]))

    def test_modes_ops_and_shapes_rejected(self):
        for mode in ('120000', '160000', '040000', '100664', '', None):
            self.denied(manifest([upsert('a.txt', mode=mode)]))
        self.denied(manifest([{'op': 'rename', 'path': 'a'}]))
        self.denied(manifest([{'op': 'delete', 'path': 'a', 'content_b64': b64('x')}]))
        self.denied(manifest([{'op': 'upsert', 'path': 'a', 'mode': '100644'}]))
        self.denied(manifest([{'op': 'upsert', 'path': 'a', 'mode': '100644', 'content_b64': b64('x'), 'x': 1}]))
        self.denied(manifest(['a.txt']))
        self.denied(manifest([upsert('a.txt'), upsert('a.txt')]))
        self.denied({'authorized_sha': SHA})
        self.denied([])

    def test_encoding_and_size_bounds_rejected(self):
        self.denied(manifest([{'op': 'upsert', 'path': 'a', 'mode': '100644', 'content_b64': '!!notbase64'}]))
        self.denied(manifest([upsert('big.bin', 'x' * (publisher.MAX_FILE_BYTES + 1))]))
        self.denied(manifest([upsert(f'f{i}.txt') for i in range(publisher.MAX_FILES + 1)]))
        self.denied(manifest([]))
        count = publisher.MAX_TOTAL_BYTES // publisher.MAX_FILE_BYTES + 1
        self.denied(manifest([upsert(f'f{i}.bin', 'x' * publisher.MAX_FILE_BYTES) for i in range(count)]))

    def test_manifest_must_match_this_authorization(self):
        self.denied(manifest([upsert('a')], authorized_sha=OTHER))
        self.denied(manifest([upsert('a')], head_ref='main'))
        self.denied(manifest([upsert('a')], number=8))


class FakeGitHub(BaseHTTPRequestHandler):
    routes: dict = {}
    requests: list = []
    counters: dict = {}

    def handle_any(self, method):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        FakeGitHub.requests.append((method, self.path, body))
        hit = self.routes.get((method, self.path), (404, {'message': 'Not Found'}))
        if callable(hit):
            hit = hit(body)
        status, payload = hit
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self.handle_any('GET')

    def do_POST(self):  # noqa: N802
        self.handle_any('POST')

    def do_PATCH(self):  # noqa: N802
        self.handle_any('PATCH')

    def log_message(self, *args):
        pass


class ProcessCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeGitHub)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        FakeGitHub.requests = []

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = {'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
               'GITHUB_API_URL': f'http://127.0.0.1:{self.port}', 'CLAUDE_GUARD_TOKEN': 'x',
               'CLAUDE_GUARD_REPOSITORY': REPO, 'CLAUDE_GUARD_NUMBER': '7', 'CLAUDE_GUARD_COMMENT_ID': '99',
               'CLAUDE_GUARD_HEAD_REF': 'feature/x', 'CLAUDE_GUARD_AUTHORIZED_SHA': SHA,
               'CLAUDE_PUBLISH_SNAPSHOT': str(self.dir / 'snapshot.json'),
               'CLAUDE_PUBLISH_CHANGES': str(self.dir / 'changes.json'),
               'GITHUB_OUTPUT': str(self.dir / 'output'), 'RUNNER_TEMP': str(self.dir)}
        env.update(extra)
        return env

    def run_script(self, mode, **extra):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), mode], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)


class CollectTests(ProcessCase):
    """Runs prepare/collect against a real git checkout the way the read-only job does."""

    def git(self, *args):
        return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'user.name=t', '-c', 'user.email=t@t',
                               '-C', str(self.pr_dir), *args], check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        path = self.pr_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, newline='')

    def setUp(self):
        super().setUp()
        self.pr_dir = self.dir / 'pr'
        self.pr_dir.mkdir()
        self.git('init', '-q')
        for rel in ('a.txt', 'old.txt', 'keep.txt', '.github/workflows/x.yml', '.claude/settings.json',
                    '.mcp.json', 'bunfig.toml', 'CLAUDE.md'):
            self.write(rel, f'orig {rel}\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        self.head = self.git('rev-parse', 'HEAD').strip()

    def collect_env(self):
        return {'CLAUDE_PUBLISH_WORKDIR': str(self.pr_dir), 'CLAUDE_GUARD_AUTHORIZED_SHA': self.head}

    def test_prepare_removes_executable_config_but_keeps_workflows_and_instructions_readable(self):
        result = self.run_script('prepare', **self.collect_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        for rel in ('.claude/settings.json', '.mcp.json', 'bunfig.toml'):
            self.assertFalse((self.pr_dir / rel).exists(), rel)
        for rel in ('.github/workflows/x.yml', 'CLAUDE.md', 'a.txt'):
            self.assertTrue((self.pr_dir / rel).exists(), rel)

    def test_prepare_refuses_a_checkout_that_is_not_the_authorized_commit(self):
        result = self.run_script('prepare', CLAUDE_PUBLISH_WORKDIR=str(self.pr_dir),
                                 CLAUDE_GUARD_AUTHORIZED_SHA=OTHER)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.dir / 'snapshot.json').exists())

    def test_collect_publishes_only_safe_edits(self):
        self.assertEqual(self.run_script('prepare', **self.collect_env()).returncode, 0)
        self.write('a.txt', 'changed\n')
        self.write('new/dir/n.txt', 'new\n')
        (self.pr_dir / 'old.txt').unlink()
        self.write('.github/workflows/x.yml', 'evil\n')          # protected: dropped
        self.write('.claude/settings.json', '{"hooks": {}}\n')   # executable config: dropped
        self.write('bunfig.toml', 'preload = ["evil.js"]\n')     # executable config: dropped
        self.write('CLAUDE.md', 'injected instructions\n')       # protected: dropped
        self.write('sub/CLAUDE.md', 'injected\n')                # protected: dropped
        self.write('__pycache__/x.pyc', 'junk')                  # ignored scratch
        result = self.run_script('collect', **self.collect_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads((self.dir / 'changes.json').read_text())
        self.assertEqual(doc['authorized_sha'], self.head)
        self.assertEqual([(c['op'], c['path']) for c in doc['changes']],
                         [('upsert', 'a.txt'), ('upsert', 'new/dir/n.txt'), ('delete', 'old.txt')])
        self.assertIn('has_changes=true', (self.dir / 'output').read_text())
        # Whatever collect emits must also pass the publisher's independent validation.
        publisher.validate_manifest(doc, {**TARGET, 'authorized_sha': self.head})

    def test_collect_reports_no_changes(self):
        self.assertEqual(self.run_script('prepare', **self.collect_env()).returncode, 0)
        self.write('.github/workflows/x.yml', 'evil\n')
        self.assertEqual(self.run_script('collect', **self.collect_env()).returncode, 0)
        self.assertIn('has_changes=false', (self.dir / 'output').read_text())

    def test_collect_fails_closed_on_oversize_and_symlinks(self):
        self.assertEqual(self.run_script('prepare', **self.collect_env()).returncode, 0)
        self.write('big.bin', 'x' * (publisher.MAX_FILE_BYTES + 1))
        self.assertEqual(self.run_script('collect', **self.collect_env()).returncode, 1)
        (self.pr_dir / 'big.bin').unlink()
        try:
            os.symlink('/etc/passwd', self.pr_dir / 'link')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(self.run_script('collect', **self.collect_env()).returncode, 1)

    def test_collect_without_prepare_fails_closed(self):
        self.assertEqual(self.run_script('collect', **self.collect_env()).returncode, 1)


class PublishTests(ProcessCase):
    """The publisher against a scripted GitHub API; every non-GET request is a write."""

    def route(self, pull=None, base_branch=PROTECTED_MAIN, ref=(200, None), commit=None, patch=None):
        counter = {'n': 0}

        def pulls_route(_body):
            counter['n'] += 1
            live = pull(counter['n']) if callable(pull) else (pr() if pull is None else pull)
            return 200, live

        ref_status, ref_body = ref
        FakeGitHub.routes = {
            ('GET', f'/repos/{REPO}/pulls/7'): pulls_route,
            ('GET', f'/repos/{REPO}/branches/main'): (200, base_branch),
            ('GET', f'/repos/{REPO}/git/ref/heads/feature/x'): lambda _b: (
                (200, live_ref(sha=NEW_COMMIT)) if any(r[0] == 'PATCH' for r in FakeGitHub.requests)
                else (ref_status, live_ref() if ref_body is None else ref_body)),
            ('GET', f'/repos/{REPO}/git/commits/{SHA}'): (200, {'sha': SHA, 'tree': {'sha': BASE_TREE}}),
            ('POST', f'/repos/{REPO}/git/blobs'): (201, {'sha': NEW_BLOB}),
            ('POST', f'/repos/{REPO}/git/trees'): (201, {'sha': NEW_TREE}),
            ('POST', f'/repos/{REPO}/git/commits'): (201, commit or {'sha': NEW_COMMIT,
                                                                      'verification': {'verified': True}}),
            ('PATCH', f'/repos/{REPO}/git/refs/heads/feature/x'): patch or (200, {}),
        }

    def publish(self, changes=None, doc=None, **extra):
        (self.dir / 'changes.json').write_text(json.dumps(doc if doc is not None else manifest(
            changes or [upsert('src/a.py', 'print(1)\n')])))
        return self.run_script('publish', **extra)

    def writes(self):
        return [r for r in FakeGitHub.requests if r[0] != 'GET']

    def test_happy_path_is_one_signed_commit_and_a_non_forced_fast_forward(self):
        self.route()
        result = self.publish([upsert('src/a.py'), {'op': 'delete', 'path': 'old.txt'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        methods = [(m, p.split(f'/repos/{REPO}')[1]) for m, p, _b in self.writes()]
        self.assertEqual(methods, [('POST', '/git/blobs'), ('POST', '/git/trees'), ('POST', '/git/commits'),
                                   ('PATCH', '/git/refs/heads/feature/x')])
        by = {(m, p.rsplit('/', 1)[-1]): b for m, p, b in self.writes()}
        self.assertEqual(by[('POST', 'commits')]['parents'], [SHA])
        self.assertEqual(by[('POST', 'commits')]['tree'], NEW_TREE)
        self.assertNotIn('author', by[('POST', 'commits')])  # GitHub signs it for the workflow token
        self.assertEqual(by[('POST', 'trees')]['base_tree'], BASE_TREE)
        self.assertIsNone(next(e for e in by[('POST', 'trees')]['tree'] if e['path'] == 'old.txt')['sha'])
        self.assertEqual(by[('PATCH', 'x')], {'sha': NEW_COMMIT, 'force': False})
        self.assertFalse(any(p.endswith('/git/refs') for _m, p, _b in self.writes()))

    def test_pr_closed_or_merged_before_any_write_means_no_write_and_no_branch(self):
        for pull in (pr(state='closed'), pr(state='closed', merged=True), pr(merged=True)):
            FakeGitHub.requests = []
            self.route(pull=pull)
            self.assertEqual(self.publish().returncode, 1)
            self.assertEqual(self.writes(), [])

    def test_pr_closing_between_the_first_check_and_the_ref_update_blocks_the_ref_update(self):
        self.route(pull=lambda n: pr() if n == 1 else pr(state='closed', merged=True))
        self.assertEqual(self.publish().returncode, 1)
        self.assertFalse(any(m == 'PATCH' for m, _p, _b in self.writes()))

    def test_head_moved_before_any_write_means_no_write(self):
        self.route(pull=pr(sha=OTHER), ref=(200, live_ref(sha=OTHER)))
        self.assertEqual(self.publish().returncode, 1)
        self.assertEqual(self.writes(), [])

    def test_head_moving_after_the_last_check_is_rejected_by_github_not_forced(self):
        self.route(patch=(422, {'message': 'Update is not a fast forward'}))
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        patches = [b for m, _p, b in self.writes() if m == 'PATCH']
        self.assertEqual(patches, [{'sha': NEW_COMMIT, 'force': False}])

    def test_deleted_head_branch_is_never_recreated(self):
        self.route(ref=(404, {'message': 'Not Found'}))
        self.assertEqual(self.publish().returncode, 1)
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(p.endswith('/git/refs') for _m, p, _b in FakeGitHub.requests))

    def test_unverified_commit_is_never_published(self):
        self.route(commit={'sha': NEW_COMMIT, 'verification': {'verified': False}})
        self.assertEqual(self.publish().returncode, 1)
        self.assertFalse(any(m == 'PATCH' for m, _p, _b in self.writes()))
        self.route(commit={'sha': NEW_COMMIT})
        self.assertEqual(self.publish().returncode, 1)

    def test_retargeted_or_unprotected_base_blocks_all_writes(self):
        self.route(pull=pr(base_ref='attacker/hooks'))
        self.assertEqual(self.publish().returncode, 1)
        self.route(base_branch={'name': 'main', 'protected': False})
        self.assertEqual(self.publish().returncode, 1)
        self.assertEqual(self.writes(), [])

    def test_hostile_manifests_cause_no_request_at_all(self):
        self.route()
        hostile = [
            manifest([upsert('.github/workflows/claude-agent.yml')]),
            manifest([upsert('.claude/settings.json')]),
            manifest([upsert('../escape')]),
            manifest([upsert('a', mode='120000')]),
            manifest([upsert('a')], authorized_sha=OTHER),
            manifest([upsert('a')], head_ref='main'),
            manifest([]),
        ]
        for doc in hostile:
            self.assertEqual(self.publish(doc=doc).returncode, 1)
        (self.dir / 'changes.json').write_text('{not json')
        self.assertEqual(self.run_script('publish').returncode, 1)
        (self.dir / 'changes.json').unlink()
        self.assertEqual(self.run_script('publish').returncode, 1)
        self.assertEqual(FakeGitHub.requests, [])

    def test_api_errors_and_malformed_environment_fail_closed(self):
        self.route()
        self.assertEqual(self.publish(CLAUDE_GUARD_TOKEN='').returncode, 1)
        self.assertEqual(self.publish(CLAUDE_GUARD_AUTHORIZED_SHA='abc').returncode, 1)
        self.assertEqual(self.publish(CLAUDE_GUARD_HEAD_REF='bad ref;rm').returncode, 1)
        self.assertEqual(self.publish(CLAUDE_GUARD_NUMBER='x').returncode, 1)
        self.assertEqual(self.publish(GITHUB_API_URL='http://127.0.0.1:9').returncode, 1)  # unreachable
        FakeGitHub.routes[('GET', f'/repos/{REPO}/pulls/7')] = (500, {})
        self.assertEqual(self.publish().returncode, 1)
        self.assertEqual(self.run_script('nonsense').returncode, 1)
        self.assertEqual(self.writes(), [])

    def test_preflight_mode_reports_state_with_exit_codes(self):
        self.route()
        self.assertEqual(self.run_script('preflight').returncode, 0)
        self.route(pull=pr(state='closed'))
        self.assertEqual(self.run_script('preflight').returncode, 1)


class WorkflowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / '.github/workflows/claude-agent.yml').read_text()
        self.wf = load_workflow()
        self.jobs = self.wf['jobs']
        self.claude = self.jobs['claude']
        self.publish = self.jobs['publish']
        self.action = next(s for s in self.claude['steps'] if s.get('id') == 'claude')

    def index(self, job, predicate):
        return next(i for i, s in enumerate(job['steps']) if predicate(s))

    def test_claude_job_is_read_only_and_cannot_write_anything(self):
        self.assertEqual(self.claude['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        for forbidden in ('contents: write', 'issues', 'actions', 'id-token', 'checks', 'statuses'):
            self.assertNotIn(forbidden, json.dumps(self.claude['permissions']))
        # No comment, commit-signing or file-ops path is enabled in the action.
        for key in ('use_commit_signing', 'track_progress', 'settings', 'branch_prefix', 'trigger_phrase'):
            self.assertNotIn(key, self.action['with'])
        self.assertNotIn('git push', self.text.replace('"Bash"', ''))

    def test_publish_job_holds_write_but_never_claude_or_pr_code(self):
        self.assertEqual(self.publish['permissions'], {'contents': 'write', 'pull-requests': 'read'})
        self.assertNotIn('secrets.', yaml_dump(self.publish))
        self.assertNotIn('anthropics/', yaml_dump(self.publish))
        for step in self.publish['steps']:
            ref = str(step.get('with', {}).get('ref', ''))
            self.assertNotIn('authorized_sha', ref)
            self.assertNotIn('head_ref', ref)
        checkout = next(s for s in self.publish['steps'] if 'checkout' in s.get('uses', ''))
        self.assertEqual(checkout['with']['ref'], '${{ github.sha }}')
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(self.publish['needs'], ['gate', 'claude'])
        self.assertIn("needs.claude.outputs.has_changes == 'true'", self.publish['if'])
        self.assertIn("needs.gate.outputs.authorized == 'true'", self.publish['if'])

    def test_only_the_claude_job_holds_the_claude_secret(self):
        self.assertEqual(self.text.count('secrets.'), 1)
        self.assertIn('secrets.CLAUDE_CODE_OAUTH_TOKEN', yaml_dump(self.claude))

    def test_publisher_concurrency_cannot_be_replaced_by_a_queued_run(self):
        self.assertNotEqual(self.publish['concurrency']['group'], self.claude['concurrency']['group'])
        self.assertIs(self.publish['concurrency']['cancel-in-progress'], False)

    def test_pr_checkout_is_outside_the_workspace_root_the_action_reads(self):
        pr_head = next(s for s in self.claude['steps']
                       if s.get('with', {}).get('ref') == '${{ needs.gate.outputs.authorized_sha }}')
        self.assertEqual(pr_head['with']['path'], 'pr')
        self.assertIs(pr_head['with']['persist-credentials'], False)
        run_claude = self.index(self.claude, lambda s: s.get('id') == 'claude')
        prepare = self.index(self.claude, lambda s: s.get('run', '').endswith('claude_publish.py" prepare'))
        self.assertLess(self.claude['steps'].index(pr_head), prepare)
        self.assertLess(prepare, run_claude)
        self.assertIn('./pr', self.action['with']['prompt'])

    def test_prompt_task_text_is_never_interpolated_into_shell(self):
        for job in self.jobs.values():
            for step in job['steps']:
                self.assertNotIn('needs.gate.outputs.task', step.get('run', ''))
        self.assertIn('${{ needs.gate.outputs.task }}', self.action['with']['prompt'])

    def test_agent_has_no_command_network_or_extra_tools(self):
        args = self.action['with']['claude_args']
        allowed = re.search(r'--allowedTools "([^"]*)"', args).group(1).split(',')
        self.assertEqual(sorted(allowed), ['Edit', 'Glob', 'Grep', 'Read', 'Write'])
        self.assertNotRegex(args, r'mcp__|Bash\(')
        self.assertIn('"Bash,WebFetch,WebSearch"', args)

    def test_trusted_script_comes_from_the_workflow_commit_and_runs_outside_the_pr_checkout(self):
        steps = self.claude['steps']
        trusted = self.index(self.claude, lambda s: s.get('with', {}).get('path') == 'trusted-guard')
        install = self.index(self.claude, lambda s: 'install -m 0444 trusted-guard' in s.get('run', ''))
        pr_head = self.index(self.claude, lambda s: s.get('with', {}).get('path') == 'pr')
        self.assertEqual(steps[trusted]['with']['ref'], '${{ github.sha }}')
        self.assertTrue(trusted < install < pr_head)
        self.assertIn('rm -rf trusted-guard', steps[install]['run'])
        for step in steps:
            run = step.get('run', '').replace('trusted-guard/.github/scripts/claude_publish.py', '')
            self.assertNotIn('.github/scripts/', run)
            self.assertNotIn('pr/', run)

    def test_live_revalidation_before_and_after_checkout_and_collect_only_after_success(self):
        steps = self.claude['steps']
        pre = [i for i, s in enumerate(steps) if s.get('run', '').endswith('claude_publish.py" preflight')]
        pr_head = self.index(self.claude, lambda s: s.get('with', {}).get('path') == 'pr')
        run_claude = self.index(self.claude, lambda s: s.get('id') == 'claude')
        self.assertEqual(len(pre), 2)
        self.assertLess(pre[0], pr_head)
        self.assertTrue(pr_head < pre[1] < run_claude)
        collect = steps[self.index(self.claude, lambda s: s.get('id') == 'collect')]
        self.assertEqual(collect['if'], "steps.claude.outcome == 'success'")

    def test_manifest_is_a_single_short_lived_artifact(self):
        upload = next(s for s in self.claude['steps'] if 'upload-artifact' in s.get('uses', ''))
        self.assertEqual(upload['with']['retention-days'], 1)
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        download = next(s for s in self.publish['steps'] if 'download-artifact' in s.get('uses', ''))
        self.assertEqual(upload['with']['name'], download['with']['name'])

    def test_guard_environment_comes_from_gate_outputs_only(self):
        for job in (self.claude, self.publish):
            env = job['env']
            self.assertEqual(env['CLAUDE_GUARD_NUMBER'], '${{ needs.gate.outputs.number }}')
            self.assertEqual(env['CLAUDE_GUARD_AUTHORIZED_SHA'], '${{ needs.gate.outputs.authorized_sha }}')
            self.assertEqual(env['CLAUDE_GUARD_HEAD_REF'], '${{ needs.gate.outputs.head_ref }}')
            self.assertEqual(env['CLAUDE_GUARD_TOKEN'], '${{ github.token }}')
            self.assertNotIn('runner.', json.dumps(env))  # actionlint: not available in jobs.<id>.env

    def test_gate_and_opencode_isolation_unchanged(self):
        self.assertEqual(self.wf['permissions'], {})
        self.assertEqual(self.jobs['gate']['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        self.assertEqual(self.action['with']['allowed_bots'], '')
        self.assertNotIn('OPENCODE', self.text)
        self.assertNotIn('opencode_gate', self.text)


def yaml_dump(node):
    import yaml
    return yaml.safe_dump(node)


if __name__ == '__main__':
    unittest.main()
