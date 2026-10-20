"""Runtime tests (tests v1.0): real filesystem permissions as another UID, and real git history.

These do not inspect command strings or mode bits. The filesystem tests execute operations as
an unprivileged user against the layout the workflow builds; the git tests push to real bare
repositories. When the machine cannot provide the capability (no root and no passwordless
sudo, or no way to create a user) a test is SKIPPED and says so; a skip is not a pass.
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
fsisolate = importlib.import_module('opencode_fsisolate')
publish = importlib.import_module('opencode_publish')
sandbox = importlib.import_module('opencode_sandbox')
collect = importlib.import_module('opencode_collect')

AGENT = 'ocprobe'


def privileged_prefix():
    if os.geteuid() == 0:
        return []
    if shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0:
        return ['sudo', '-n']
    return None


PRIV = privileged_prefix()


def run_priv(*cmd, check=True):
    return subprocess.run([*PRIV, *cmd], capture_output=True, text=True, check=check)


def as_agent(*cmd):
    """Run a command as the unprivileged agent user."""
    if os.geteuid() == 0:
        return subprocess.run(['setpriv', f'--reuid={AGENT}', f'--regid={AGENT}', '--init-groups', *cmd],
                              capture_output=True, text=True)
    return subprocess.run(['sudo', '-n', '-u', AGENT, *cmd], capture_output=True, text=True)


def agent_uid():
    return int(subprocess.run(['id', '-u', AGENT], capture_output=True, text=True).stdout.strip())


def priv_dir(path, owner='root', mode='755'):
    run_priv('install', '-d', '-o', owner, '-g', owner, '-m', mode, path)


@unittest.skipIf(PRIV is None, 'needs root or passwordless sudo to create a user and root-owned trees')
class RuntimeFilesystemTests(unittest.TestCase):
    """Written to run as an ordinary user with sudo (as on a GitHub runner) or as root."""

    @classmethod
    def setUpClass(cls):
        if subprocess.run(['id', AGENT], capture_output=True).returncode != 0:
            made = run_priv('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', AGENT, check=False)
            if made.returncode != 0:
                raise unittest.SkipTest(f'cannot create the test user: {made.stderr.strip()}')
        cls.tool = tempfile.mkdtemp(prefix='ocfs-tool-')
        os.chmod(cls.tool, 0o755)
        cls.tool_script = os.path.join(cls.tool, 'fsisolate.py')
        shutil.copy(SCRIPTS / 'opencode_fsisolate.py', cls.tool_script)
        os.chmod(cls.tool_script, 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tool, ignore_errors=True)

    def setUp(self):
        # Root owns the base directory, like /srv on the runner, so every ancestor of the tree is root-controlled.
        self.base = run_priv('mktemp', '-d', '/tmp/ocfs-XXXXXX').stdout.strip()
        run_priv('chmod', '755', self.base)
        self.addCleanup(lambda: run_priv('rm', '-rf', self.base, check=False))
        stage = tempfile.mkdtemp(prefix='ocfs-stage-')
        self.addCleanup(shutil.rmtree, stage, True)
        os.makedirs(os.path.join(stage, 'app'))
        Path(stage, 'app', 'main.py').write_text('print("original")\n')
        Path(stage, 'README.md').write_text('readme\n')
        self.home = os.path.join(self.base, 'agent-home')   # the agent's own writable area
        priv_dir(self.home, AGENT)
        self.review = os.path.join(self.base, 'review')
        run_priv(sys.executable, '-I', self.tool_script, 'build', stage, self.review)
        self.src = os.path.join(self.review, 'src')

    def probe(self, path, *extra):
        return as_agent(sys.executable, '-I', self.tool_script, 'probe', path, '--writable', self.home, *extra)

    def digest(self):
        return run_priv(sys.executable, '-I', self.tool_script, 'digest', self.src).stdout.strip()

    def test_reviewer_uid_is_denied_every_write_rename_replace_and_symlink(self):
        before = self.digest()
        secret = os.path.join(self.base, 'secret')
        priv_dir(secret, 'root', '700')
        result = self.probe(self.src, '--forbidden', secret)
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        passes = [l for l in lines if l.startswith('PASS')]
        self.assertGreaterEqual(len(passes), 25)
        self.assertFalse([l for l in lines if l.startswith('FAIL')])
        for expected in ('rename source tree away', 'replace source tree with symlink', 'replace source tree with attacker dir',
                         'delete source tree', 'symlink in parent directory', 'open existing file for write',
                         'write through hard-link alias', 'forbidden path unreadable', 'read existing file (positive control)'):
            self.assertTrue(any(expected in l for l in passes), expected)
        self.assertEqual(self.digest(), before)  # nothing changed

    def test_ownership_chain_of_the_built_tree_is_root_controlled(self):
        self.assertEqual(fsisolate.chain_problems(self.src), [])
        info = os.stat(self.src)
        self.assertEqual((info.st_uid, info.st_mode & 0o777), (0, 0o555))
        self.assertEqual(os.stat(self.review).st_uid, 0)

    def test_digest_detects_content_mode_and_owner_changes(self):
        before = self.digest()
        victim = os.path.join(self.src, 'app', 'main.py')
        run_priv('sh', '-c', f'printf changed > {victim}')
        changed = self.digest()
        self.assertNotEqual(changed, before)                    # content
        run_priv('chmod', '0600', victim)
        mode_changed = self.digest()
        self.assertNotEqual(mode_changed, changed)              # mode
        run_priv('chown', f'{AGENT}:{AGENT}', victim)
        self.assertNotEqual(self.digest(), mode_changed)        # owner

    def test_effective_permissions_are_as_the_reviewer_uid_not_root(self):
        # root could write everywhere; the probe result is only meaningful because it runs as the agent
        code = 'import os; print(os.getuid())'
        self.assertEqual(int(as_agent(sys.executable, '-I', '-c', code).stdout.strip()), agent_uid())
        self.assertNotEqual(agent_uid(), 0)

    def test_agent_cannot_modify_then_restore(self):
        victim = os.path.join(self.src, 'app', 'main.py')
        before = self.digest()
        script = (
            "import os, shutil, sys\n"
            f"victim = {victim!r}\nsrc = {self.src!r}\nhome = {self.home!r}\n"
            "original = open(victim).read()\n"
            "steps = []\n"
            "for name, op in (\n"
            "  ('overwrite', lambda: open(victim, 'w').write('evil')),\n"
            "  ('chmod then overwrite', lambda: (os.chmod(victim, 0o666), open(victim, 'w').write('evil'))),\n"
            "  ('move aside', lambda: os.rename(src, src + '.orig')),\n"
            "  ('copy-modify-swap', lambda: (shutil.copytree(src, home + '/copy'), open(home + '/copy/app/main.py', 'w').write('evil'), os.rename(home + '/copy', src))),\n"
            "):\n"
            "  try:\n    op(); steps.append((name, 'SUCCEEDED'))\n"
            "  except OSError as e:\n    steps.append((name, 'denied'))\n"
            "print(steps)\n"
            "print('SAME' if open(victim).read() == original else 'CHANGED')\n")
        out = as_agent(sys.executable, '-I', '-c', script).stdout
        self.assertNotIn('SUCCEEDED', out, out)
        self.assertIn('SAME', out)
        self.assertEqual(self.digest(), before)

    def test_symlink_substitution_of_the_source_directory_is_denied(self):
        elsewhere = os.path.join(self.home, 'elsewhere')
        priv_dir(elsewhere, AGENT)
        script = ("import os\n"
                  f"os.symlink({elsewhere!r}, {self.home!r} + '/link')\n"
                  f"os.rename({self.home!r} + '/link', {self.src!r})\n")
        result = as_agent(sys.executable, '-I', '-c', script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PermissionError', result.stderr)
        self.assertFalse(os.path.islink(self.src))

    def test_old_layout_with_an_agent_owned_parent_is_caught_by_chain_and_probe(self):
        self.assertEqual(fsisolate.chain_problems(self.src), [])
        run_priv('chown', f'{AGENT}:{AGENT}', self.review)       # the previous layout
        problems = fsisolate.chain_problems(self.src)
        self.assertTrue(any(self.review in p for p in problems), problems)
        probe = self.probe(self.src)
        self.assertEqual(probe.returncode, 1, probe.stdout)
        failed = [l for l in probe.stdout.splitlines() if l.startswith('FAIL')]
        self.assertTrue(any('rename source tree' in l for l in failed), probe.stdout)
        self.assertTrue(any('replace source tree with symlink' in l for l in failed), probe.stdout)

    def test_old_layout_really_allows_replacement_and_restoration_that_a_digest_misses(self):
        # Reproduces the review finding: with an agent-owned parent the agent swaps the directory,
        # edits it, restores the content, and a digest of the content alone cannot tell the difference.
        before = self.digest()
        run_priv('chown', f'{AGENT}:{AGENT}', self.review)
        script = (
            "import os, shutil\n"
            f"src = {self.src!r}\n"
            "os.rename(src, src + '.moved')\n"
            "shutil.copytree(src + '.moved', src)\n"
            "print('replaced')\n")
        result = as_agent(sys.executable, '-I', '-c', script)
        self.assertIn('replaced', result.stdout, result.stderr)   # the vulnerability is real in that layout
        self.assertEqual(os.stat(self.src).st_uid, agent_uid())   # the tree is now agent-owned, i.e. writable
        self.assertNotEqual(self.digest(), before)                # ownership is part of the digest
        self.assertNotEqual(fsisolate.chain_problems(self.src), [])

    def test_parent_probe_for_the_implementation_work_tree(self):
        impl = os.path.join(self.base, 'impl')
        work = os.path.join(impl, 'work')
        priv_dir(impl)
        priv_dir(work, AGENT)
        forbidden = os.path.join(self.base, 'secret')
        priv_dir(forbidden, 'root', '700')
        ok = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', work, '--writable', self.home,
                      '--forbidden', forbidden)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        # agent-owned parent: the work tree can be renamed/replaced -> detected
        run_priv('chown', f'{AGENT}:{AGENT}', impl)
        bad = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', work, '--writable', self.home)
        self.assertEqual(bad.returncode, 1, bad.stdout)

    def test_collector_refuses_a_worktree_the_agent_could_swap(self):
        scratch = tempfile.mkdtemp(prefix='ocfs-link-')
        self.addCleanup(shutil.rmtree, scratch, True)
        link = os.path.join(scratch, 'worktree-link')
        os.symlink(self.src, link)
        state = os.path.join(scratch, 'state')
        os.makedirs(state)
        with self.assertRaises(sandbox.SandboxError):
            collect.collect(link, state, os.path.join(scratch, 'out'))
        # with the workflow's setting, an agent-owned ancestor is refused too
        run_priv('chown', f'{AGENT}:{AGENT}', self.review)
        old = os.environ.get('OPENCODE_REQUIRE_ROOT_CHAIN')
        os.environ['OPENCODE_REQUIRE_ROOT_CHAIN'] = '1'
        try:
            with self.assertRaises(sandbox.SandboxError):
                collect.collect(self.src, state, os.path.join(scratch, 'out2'))
        finally:
            if old is None:
                os.environ.pop('OPENCODE_REQUIRE_ROOT_CHAIN')
            else:
                os.environ['OPENCODE_REQUIRE_ROOT_CHAIN'] = old

    def test_read_only_bind_mount_when_the_platform_allows_it(self):
        mounted = run_priv(sys.executable, '-I', self.tool_script, 'mount', self.review, check=False)
        if 'active' not in mounted.stdout:
            self.skipTest('bind mounts are not permitted in this environment')
        self.addCleanup(lambda: run_priv('umount', self.src, check=False))
        write = run_priv('sh', '-c', f'echo x > {self.src}/app/main.py', check=False)   # even root cannot write through it
        self.assertNotEqual(write.returncode, 0)
        self.assertIn('Read-only file system', write.stderr)


class RealGitRepo:
    """A bare 'remote' plus helpers, for realistic history transitions."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.bare = os.path.join(tmp, 'remote.git')
        self.work = os.path.join(tmp, 'work')
        self.git('init', '-q', '--bare', '-b', 'main', self.bare, cwd=tmp)
        self.git('init', '-q', '-b', 'main', self.work, cwd=tmp)
        for key, value in (('user.name', 't'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
            self.git('config', key, value, cwd=self.work)
        self.git('remote', 'add', 'origin', f'file://{self.bare}', cwd=self.work)
        self.url = f'file://{self.bare}'

    def git(self, *args, cwd=None, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update({'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'})   # no global git config on CI
        result = subprocess.run(['git', *args], cwd=cwd or self.work, capture_output=True, text=True, env=env)
        if check and result.returncode:
            raise AssertionError(f'git {args} failed: {result.stderr}')
        return result.stdout.strip()

    def commit(self, name, parent=None):
        if parent:
            self.git('checkout', '-q', '--detach', parent)
        Path(self.work, name).write_text(name)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', name)
        return self.git('rev-parse', 'HEAD')

    def push(self, sha, ref, force=False):
        self.git('push', '-q', *(['--force'] if force else []), 'origin', f'{sha}:refs/heads/{ref}')

    def remote_tip(self, ref):
        result = self.git('rev-parse', '--verify', '-q', f'refs/heads/{ref}', cwd=self.bare, check=False)
        return result or None

    def history(self):
        """ancestor A <- base B <- new N (the verified commit), all on a PR branch 'feature'."""
        a = self.commit('a')
        b = self.commit('b')
        self.push(b, 'feature')
        self.push(a, 'main')
        n = self.commit('n', parent=b)
        self.push(n, 'opencode/pr9-1-1')
        return a, b, n

    def cas(self, branch, expected, new, staging='opencode/pr9-1-1'):
        fresh = tempfile.mkdtemp(dir=self.tmp)   # a clone-less checkout, as in the publish job
        subprocess.run(['git', 'init', '-q', fresh], check=True, capture_output=True)
        publish.git_cas_push(fresh, self.url, None, branch, expected, new, staging)


class ExactShaConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ocgit-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = RealGitRepo(self.tmp)
        self.a, self.b, self.n = self.repo.history()

    def test_unchanged_branch_is_updated_exactly(self):
        self.repo.cas('feature', self.b, self.n)
        self.assertEqual(self.repo.remote_tip('feature'), self.n)

    def test_concurrent_push_of_a_descendant_is_rejected(self):
        other = self.repo.commit('c', parent=self.b)
        self.repo.push(other, 'feature')
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.n)
        self.assertEqual(self.repo.remote_tip('feature'), other)

    def test_concurrent_reset_to_an_ancestor_is_rejected_although_it_would_fast_forward(self):
        self.repo.push(self.a, 'feature', force=True)          # someone force-resets the branch to A
        # The new commit descends from A, so a fast-forward-only update WOULD succeed and undo the reset:
        ancestor_check = subprocess.run(['git', 'merge-base', '--is-ancestor', self.a, self.n], cwd=self.repo.work)
        self.assertEqual(ancestor_check.returncode, 0)
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.n)
        self.assertEqual(self.repo.remote_tip('feature'), self.a)   # the reset is preserved

    def test_reset_to_an_unrelated_commit_is_rejected(self):
        self.repo.git('checkout', '-q', '--orphan', 'other')
        self.repo.git('rm', '-rfq', '.', check=False)
        Path(self.repo.work, 'u').write_text('u')
        self.repo.git('add', '-A')
        self.repo.git('commit', '-q', '-m', 'unrelated')
        unrelated = self.repo.git('rev-parse', 'HEAD')
        self.repo.push(unrelated, 'feature', force=True)
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.n)
        self.assertEqual(self.repo.remote_tip('feature'), unrelated)

    def test_deleted_branch_is_not_recreated(self):
        self.repo.git('push', '-q', 'origin', ':refs/heads/feature')
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.n)
        self.assertIsNone(self.repo.remote_tip('feature'))

    def test_server_side_race_after_the_client_check_is_rejected_by_receive_pack(self):
        # A pre-receive hook resets the branch to A after git's own lease check has passed,
        # which is the window an advertisement-only check cannot close.
        hook = os.path.join(self.repo.bare, 'hooks', 'pre-receive')
        Path(hook).write_text(f'#!/bin/sh\nunset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n'
                              f'git update-ref refs/heads/feature {self.a}\n')
        os.chmod(hook, 0o755)
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.n)
        self.assertEqual(self.repo.remote_tip('feature'), self.a)

    def test_refuses_non_sha_expectations_and_a_staging_ref_that_is_not_the_verified_commit(self):
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', 'feature', self.n)
        with self.assertRaises(publish.PushRejected):
            self.repo.cas('feature', self.b, self.a)   # staging branch holds n, not a
        self.assertEqual(self.repo.remote_tip('feature'), self.b)


class RealGitApi:
    """GitHub REST/GraphQL surface backed by a real bare repository."""

    def __init__(self, repo, bot='bot[bot]', verified=True):
        self.repo, self.bot, self.verified, self.calls = repo, bot, verified, []
        self.before_push = None
        self.pr_head_ref = 'feature'

    def _tip(self, ref):
        return self.repo.remote_tip(ref)

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == 'GET' and path == '':
            return {'default_branch': 'main', 'full_name': 'o/r'}
        if path == 'branches/main':
            return {'protected': True, 'commit': {'sha': self._tip('main')}}
        if path.startswith('branches/'):
            return {'protected': False, 'commit': {'sha': self._tip(path[len('branches/'):])}}
        if path.startswith('git/ref/heads/'):
            tip = self._tip(path[len('git/ref/heads/'):])
            if tip is None:
                err = publish.PublishError('HTTP 404')
                err.status = 404
                raise err
            return {'object': {'sha': tip}}
        if path == 'pulls/9':
            return {'number': 9, 'state': 'open', 'merged': False, 'base': {'repo': {'full_name': 'o/r'}},
                    'head': {'ref': self.pr_head_ref, 'sha': self._tip(self.pr_head_ref), 'repo': {'full_name': 'o/r'}}}
        if path.startswith('compare/'):
            old, new = path[len('compare/'):].split('...')
            ahead = self.repo.git('merge-base', '--is-ancestor', old, new, cwd=self.repo.bare, check=False) == '' and \
                subprocess.run(['git', 'merge-base', '--is-ancestor', old, new], cwd=self.repo.bare).returncode == 0
            return {'status': 'ahead' if ahead and old != new else 'identical' if old == new else 'diverged',
                    'behind_by': 0 if ahead else 1}
        if path.startswith('commits/'):
            return {'commit': {'verification': {'verified': self.verified, 'reason': 'valid' if self.verified else 'unsigned'}},
                    'author': {'login': self.bot}}
        if method == 'POST' and path == 'git/refs':
            self.repo.git('update-ref', body['ref'], body['sha'], cwd=self.repo.bare)
            return {}
        if method == 'DELETE' and path.startswith('git/refs/heads/'):
            self.repo.git('update-ref', '-d', 'refs/heads/' + path[len('git/refs/heads/'):], cwd=self.repo.bare)
            return {}
        if path.startswith('pulls/9/reviews'):
            return []
        return {}

    def graphql(self, query, variables):
        data = variables['input']
        branch = data['branch']['branchName']
        if self._tip(branch) != data['expectedHeadOid']:
            raise publish.PublishError('GraphQL error: expected head mismatch')
        tree = self.repo.git('rev-parse', f"{data['expectedHeadOid']}^{{tree}}", cwd=self.repo.bare)
        commit = self.repo.git('commit-tree', tree, '-p', data['expectedHeadOid'], '-m', data['message']['headline'],
                               cwd=self.repo.bare)
        self.repo.git('update-ref', f'refs/heads/{branch}', commit, cwd=self.repo.bare)
        return {'createCommitOnBranch': {'commit': {'oid': commit}}}


PR_ENV = {'GITHUB_REPOSITORY': 'o/r', 'DEFAULT_BRANCH': 'main', 'EVENT_KIND': 'pr', 'TARGET_NUMBER': '9',
          'GENERATION_STATUS': 'success', 'RUN_URL': 'https://x', 'RUN_ID': '1', 'RUN_ATTEMPT': '1',
          'ACTOR_LOGIN': 'me', 'EXPECT_BOT_LOGIN': 'bot[bot]', 'TASK': 't', 'MODEL': 'opencode/glm-5.3-flash-free',
          'CATEGORY': 'general', 'ROUTE_REASON': 'r', 'PR_HEAD_REF': 'feature', 'SESSION_ID': 'ses_impl000111'}


class PublishAgainstRealHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ocpub-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = RealGitRepo(self.tmp)
        a = self.repo.commit('a')
        b = self.repo.commit('b')
        self.repo.push(a, 'main')
        self.repo.push(b, 'feature')
        self.a, self.b = a, b
        self.api = RealGitApi(self.repo)
        self.env = {**PR_ENV, 'AUTHORIZED_SHA': b}
        self.plan = {'base_sha': b, 'additions': [{'path': 'x.txt', 'contents': 'eA=='}], 'deletions': []}
        self.pushes = []

    def git_push(self, branch, expected, new, staging):
        if self.api.before_push:
            self.api.before_push()
        self.pushes.append((branch, expected, new))
        fresh = tempfile.mkdtemp(dir=self.tmp)       # an unrelated checkout, as in the publish job
        subprocess.run(['git', 'init', '-q', fresh], check=True, capture_output=True)
        publish.git_cas_push(fresh, self.repo.url, None, branch, expected, new, staging)

    def run_publish(self, evidence=None):
        return publish.publish(self.api, self.env, self.plan, sleep=lambda _s: None,
                               git_push=self.git_push, evidence=evidence if evidence is not None else {})

    def test_happy_path_moves_the_branch_exactly_once_and_removes_staging(self):
        result = self.run_publish()
        self.assertEqual(self.repo.remote_tip('feature'), result['commit'])
        self.assertIsNone(self.repo.remote_tip('opencode/pr9-1-1'))
        self.assertEqual(self.pushes, [('feature', self.b, result['commit'])])

    def test_concurrent_reset_to_an_ancestor_between_verification_and_update_is_not_undone(self):
        self.api.before_push = lambda: self.repo.push(self.a, 'feature', force=True)
        evidence = {}
        with self.assertRaises(Exception) as raised:
            self.run_publish(evidence)
        self.assertEqual(self.repo.remote_tip('feature'), self.a)                 # their reset survives
        self.assertIsNone(self.repo.remote_tip('opencode/pr9-1-1'))               # staging cleaned up
        self.assertEqual(evidence['pr_branch_observed_after_failure'], self.a)    # real state recorded
        self.assertIsInstance(raised.exception, publish.PartialPublication)       # not claimed "unchanged"
        self.assertIn(self.a, str(raised.exception))

    def test_concurrent_descendant_push_is_not_overwritten(self):
        other = {}
        def move():
            other['sha'] = self.repo.commit('c', parent=self.b)
            self.repo.push(other['sha'], 'feature')
        self.api.before_push = move
        with self.assertRaises(publish.PublishError):
            self.run_publish()
        self.assertEqual(self.repo.remote_tip('feature'), other['sha'])

    def test_final_authorization_aborts_before_any_push_when_the_pr_changed(self):
        original = self.api.call

        def drift(method, path, body=None):
            if path == 'pulls/9' and any(p == 'commits/' + (self.repo.remote_tip('opencode/pr9-1-1') or '') for _m, p in self.api.calls):
                result = original(method, path, body)
                result['state'] = 'closed'      # closed after verification, before the update
                return result
            return original(method, path, body)
        self.api.call = drift
        with self.assertRaises(publish.PublishError) as raised:
            self.run_publish()
        self.assertEqual(self.pushes, [])
        self.assertIn('not open', str(raised.exception))
        self.assertEqual(self.repo.remote_tip('feature'), self.b)                # verified, not assumed
        self.assertIsNone(self.repo.remote_tip('opencode/pr9-1-1'))

    def test_head_ref_retargeted_to_an_unrelated_branch_is_refused_without_mutation(self):
        other = self.repo.commit('z', parent=self.a)
        self.repo.push(other, 'unrelated')
        self.api.pr_head_ref = 'unrelated'            # PR 9 now points at a different branch
        with self.assertRaises(publish.PublishError):
            self.run_publish()
        self.assertEqual([m for m, _p in self.api.calls if m in ('POST', 'DELETE')], [])
        self.assertEqual(self.repo.remote_tip('unrelated'), other)

    def test_unverified_commit_never_reaches_the_pr_branch(self):
        self.api.verified = False
        with self.assertRaises(publish.PublishError):
            self.run_publish()
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.repo.remote_tip('feature'), self.b)
        self.assertIsNone(self.repo.remote_tip('opencode/pr9-1-1'))


if __name__ == '__main__':
    unittest.main()
