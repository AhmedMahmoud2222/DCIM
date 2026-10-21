"""Runtime tests (tests v2.0): real filesystem permissions as another UID, and real git history.

These do not inspect command strings or mode bits. The filesystem tests execute operations as
an unprivileged user against the layout the workflow builds; the git tests push to real bare
repositories.

Privileged tests (another UID, root-owned trees, real mounts) run only on a DISPOSABLE host:
`OPENCODE_DISPOSABLE_HOST=1`, set by CI on GitHub-hosted runners, whose VM is discarded after the
job. They create the `ocprobe` account and root-owned or mounted fixtures under
`OPENCODE_DISPOSABLE_ROOT` and never remove them: the VM teardown does. No test unmounts anything
or deletes a privileged tree. Without the variable they are skipped, which CI treats as a failure
(see tests/deployment/opencode_validation/). With the variable but without root or passwordless
sudo, importing this module fails instead of skipping.
"""
import importlib
import os
import errno
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
fsisolate = importlib.import_module('opencode_fsisolate')
publish = importlib.import_module('opencode_publish')
sandbox = importlib.import_module('opencode_sandbox')
collect = importlib.import_module('opencode_collect')

AGENT = 'ocprobe'
DISPOSABLE = os.environ.get('OPENCODE_DISPOSABLE_HOST') == '1'
DISPOSABLE_ROOT = os.environ.get('OPENCODE_DISPOSABLE_ROOT', '/srv/opencode-disposable')   # root-controlled ancestors
PRIVILEGED_SKIP = 'privileged test: runs only on a disposable host (OPENCODE_DISPOSABLE_HOST=1)'


def privileged_prefix():
    if os.geteuid() == 0:
        return []
    if shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0:
        return ['sudo', '-n']
    return None


PRIV = privileged_prefix()
if DISPOSABLE and PRIV is None:
    raise RuntimeError('OPENCODE_DISPOSABLE_HOST=1 but neither root nor passwordless sudo is available')


def temp_dir(case, prefix):
    """An unprivileged temporary directory removed by the stdlib after the test; removal errors surface."""
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(holder.cleanup)
    return holder.name


def class_temp_dir(cls, prefix):
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    cls.addClassCleanup(holder.cleanup)
    return holder.name


def disposable_dir(prefix):
    """A root-owned directory for privileged fixtures. It is never deleted by the tests: the disposable VM is."""
    run_priv('install', '-d', '-o', 'root', '-g', 'root', '-m', '755', DISPOSABLE_ROOT)
    path = run_priv('mktemp', '-d', os.path.join(DISPOSABLE_ROOT, prefix + 'XXXXXXXX')).stdout.strip()
    run_priv('chmod', '755', path)                 # traversable by the agent, like /srv on the runner
    return path


def create_agent_account():
    """Create the test account once per disposable host. An account that already exists is not ours to use."""
    if subprocess.run(['getent', 'passwd', AGENT], capture_output=True).returncode == 0:
        if os.environ.get('OPENCODE_TEST_ACCOUNT_CREATED') == AGENT:
            return
        raise RuntimeError(f'account {AGENT} already exists on this host; refusing to use an account the suite did not create')
    run_priv('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', AGENT)
    os.environ['OPENCODE_TEST_ACCOUNT_CREATED'] = AGENT


def mounted(path):
    """True when `path` is a mount point, according to findmnt (util-linux)."""
    return subprocess.run(['findmnt', '--mountpoint', path], capture_output=True).returncode == 0


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


@unittest.skipUnless(DISPOSABLE, PRIVILEGED_SKIP)
class RuntimeFilesystemTests(unittest.TestCase):
    """Written to run as an ordinary user with sudo (as on a GitHub runner) or as root."""

    @classmethod
    def setUpClass(cls):
        create_agent_account()
        cls.tool = class_temp_dir(cls, 'ocfs-tool-')
        os.chmod(cls.tool, 0o755)
        cls.tool_script = os.path.join(cls.tool, 'fsisolate.py')
        shutil.copy(SCRIPTS / 'opencode_fsisolate.py', cls.tool_script)
        os.chmod(cls.tool_script, 0o755)

    def setUp(self):
        # Root owns the base directory, like /srv on the runner, so every ancestor of the tree is root-controlled.
        self.base = disposable_dir('ocfs-')
        stage = temp_dir(self, 'ocfs-stage-')
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
        self.assertGreaterEqual(len(passes), 12)
        self.assertFalse([l for l in lines if l.startswith('FAIL')])
        for expected in ('rename source tree away', 'replace source tree with symlink', 'replace source tree with attacker dir',
                         'delete source tree', 'create entry or symlink in parent directory',
                         'write/truncate/append/chmod of any source file', 'forbidden path unreadable',
                         'read existing file (positive control)'):
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

    # ---- preflight must never modify the trees it inspects --------------------------------

    @staticmethod
    def snapshot(root, as_root=True):
        """Names, types, modes, owners, link counts, mtimes, inodes, symlink targets and sha256 of bytes."""
        script = (
            "import hashlib, json, os, stat, sys\n"
            "root = sys.argv[1]\nout = {}\n"
            "for base, dirs, files in os.walk(root, followlinks=False):\n"
            "  for name in ['.'] + sorted(dirs + files):\n"
            "    full = os.path.normpath(os.path.join(base, name))\n"
            "    i = os.lstat(full)\n"
            "    rec = [stat.S_IFMT(i.st_mode), stat.S_IMODE(i.st_mode), i.st_uid, i.st_gid, i.st_nlink, i.st_mtime_ns, i.st_ino, i.st_size]\n"
            "    if stat.S_ISLNK(i.st_mode): rec.append(os.readlink(full))\n"
            "    elif stat.S_ISREG(i.st_mode): rec.append(hashlib.sha256(open(full,'rb').read()).hexdigest())\n"
            "    out[os.path.relpath(full, root)] = rec\n"
            "print(json.dumps(out, sort_keys=True))\n")
        return run_priv(sys.executable, '-I', '-c', script, root).stdout

    def populate(self, root, owner):
        """A nested tree with representative files: text, binary, executable, empty, dotfile, odd names."""
        files = {
            'README.md': (b'# project\n', 0o644),
            'app/main.py': (b'print("original")\n', 0o644),
            'app/pkg/__init__.py': (b'', 0o644),
            'app/pkg/data.bin': (bytes(range(256)) * 8, 0o600),
            'app/pkg/deep/er/est/leaf.txt': (b'leaf\n', 0o640),
            'scripts/run.sh': (b'#!/bin/sh\necho run\n', 0o755),
            '.github/CODEOWNERS': (b'* @owner\n', 0o644),
            'docs/name with spaces.md': (b'spaces\n', 0o644),
            'docs/\u00fcnicode.txt': ('\u00fc\n'.encode(), 0o644),
        }
        for rel, (data, mode) in files.items():
            full = os.path.join(root, rel)
            run_priv('mkdir', '-p', os.path.dirname(full))
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(data)
            tmp.close()
            os.chmod(tmp.name, 0o644)
            run_priv('install', '-m', f'{mode:o}', tmp.name, full)
            os.unlink(tmp.name)
        run_priv('mkdir', '-p', os.path.join(root, 'empty-dir'))
        run_priv('ln', '-s', 'app/main.py', os.path.join(root, 'link-to-main'))
        run_priv('chown', '-R', f'{owner}:{owner}', root)
        run_priv('touch', '-d', '2020-01-02 03:04:05', os.path.join(root, 'README.md'))

    def agent_layout(self, parent_owner='root'):
        impl = os.path.join(self.base, 'impl')
        work = os.path.join(impl, 'work')
        priv_dir(impl, parent_owner)
        priv_dir(work, AGENT)
        self.populate(work, AGENT)
        return impl, work

    def test_parent_preflight_leaves_a_populated_work_tree_exactly_as_it_was(self):
        _impl, work = self.agent_layout()
        before = self.snapshot(work)
        self.assertGreater(len(before), 400)
        forbidden = os.path.join(self.base, 'secret')
        priv_dir(forbidden, 'root', '700')
        result = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', work, '--writable', self.home,
                          '--forbidden', forbidden)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(work), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(work))), ['work'])    # nothing added beside it either
        self.assertEqual(os.listdir(self.home), [])                              # nor in the agent's home

    def test_parent_preflight_failure_leaves_the_tree_intact_and_reports_accurately(self):
        # Worst case: the agent owns the parent, so a destructive probe WOULD have been able to delete or swap the tree.
        impl, work = self.agent_layout(parent_owner=AGENT)
        before = self.snapshot(work)
        result = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', work, '--writable', self.home)
        self.assertEqual(result.returncode, 1, result.stdout)
        failed = [l for l in result.stdout.splitlines() if l.startswith('FAIL')]
        for name in ('rename work tree away', 'replace work tree with symlink', 'delete work tree'):
            self.assertTrue(any(name in l for l in failed), (name, result.stdout))
        self.assertIn('isolation check(s) failed', result.stdout)
        self.assertEqual(self.snapshot(work), before)
        self.assertEqual(sorted(os.listdir(impl)), ['work'])

    def test_reviewer_preflight_leaves_the_populated_source_tree_exactly_as_it_was(self):
        self.review2 = os.path.join(self.base, 'review2')
        stage = temp_dir(self, 'ocfs-stage2-')
        self.populate_plain(stage)
        run_priv(sys.executable, '-I', self.tool_script, 'build', stage, self.review2)
        src = os.path.join(self.review2, 'src')
        before = self.snapshot(src)
        result = self.probe(src)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.snapshot(src), before)
        self.assertEqual(sorted(os.listdir(self.review2)), ['src'])

    def populate_plain(self, stage):
        for rel, data in {'a/b/c/d.txt': b'd\n', 'a/b/e.bin': bytes(range(255)), 'top.py': b'x = 1\n', '.hidden': b'h\n'}.items():
            full = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            Path(full).write_bytes(data)
        os.chmod(os.path.join(stage, 'top.py'), 0o755)

    def test_reviewer_preflight_failure_on_a_writable_layout_changes_nothing(self):
        # Previous behaviour: with an agent-owned parent the probe itself renamed/replaced/deleted the tree.
        before = self.snapshot(self.src)
        run_priv('chown', f'{AGENT}:{AGENT}', self.review)
        run_priv('chown', '-R', f'{AGENT}:{AGENT}', self.src)         # also make every file and directory agent-writable
        run_priv('chmod', '-R', 'u+w', self.src)
        writable_before = self.snapshot(self.src)
        self.assertNotEqual(writable_before, before)
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        failed = [l for l in result.stdout.splitlines() if l.startswith('FAIL')]
        self.assertTrue(any('write/truncate/append/chmod of any source file' in l for l in failed), result.stdout)
        self.assertTrue(any('inside any source directory' in l for l in failed), result.stdout)
        self.assertEqual(self.snapshot(self.src), writable_before)    # still exactly what the failing preflight found
        self.assertEqual(sorted(os.listdir(self.review)), ['src'])

    def test_preflight_does_not_follow_a_symlink_swapped_in_for_the_tree(self):
        _impl, work = self.agent_layout()
        target = os.path.join(self.base, 'victim-dir')
        priv_dir(target, AGENT)
        run_priv('sh', '-c', 'echo keep > "$0/keep.txt"', target)
        run_priv('rm', '-rf', work)
        run_priv('ln', '-s', target, work)
        before = self.snapshot(target)
        result = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', work, '--writable', self.home)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('not a symlink' in l for l in result.stdout.splitlines() if l.startswith('FAIL')), result.stdout)
        self.assertEqual(self.snapshot(target), before)

    # ---- agent-owned entries inside the protected source (Codex P1) -------------------------

    def own(self, path, mode):
        run_priv('chown', f'{AGENT}:{AGENT}', path)
        run_priv('chmod', mode, path)

    def modify_and_restore_as_agent(self, victim):
        """What an owner can do: chmod, edit, restore the mode and bytes. Returns the outcome text."""
        script = ("import os, stat, sys\n"
                  f"v = {victim!r}\n"
                  "orig = open(v, 'rb').read(); mode = stat.S_IMODE(os.stat(v).st_mode)\n"
                  "os.chmod(v, 0o644); open(v, 'wb').write(b'evil'); open(v, 'wb').write(orig); os.chmod(v, mode)\n"
                  "print('MODIFIED-AND-RESTORED')\n")
        return as_agent(sys.executable, '-I', '-c', script)

    def test_agent_owned_0444_file_is_rejected_and_the_attack_really_works(self):
        victim = os.path.join(self.src, 'app', 'main.py')
        self.own(victim, '0444')
        before = self.snapshot(self.src)
        digest = self.digest()
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        failed = [l for l in result.stdout.splitlines() if l.startswith('FAIL')]
        self.assertTrue(any('owned by the agent' in l and victim in l for l in failed), result.stdout)
        self.assertEqual(self.snapshot(self.src), before)             # the preflight itself changed nothing
        # The weakness is real: the owner chmods, edits and restores, and the content digest cannot see it
        # because the post-run digest matches again except for ownership, which the probe now rejects up front.
        attack = self.modify_and_restore_as_agent(victim)
        self.assertIn('MODIFIED-AND-RESTORED', attack.stdout, attack.stderr)
        self.assertEqual(self.digest(), digest)                       # same bytes, mode and owner as before the attack

    def test_agent_owned_0555_directory_is_rejected_at_any_depth(self):
        for rel in ('app', os.path.join('app', 'sub')):
            run_priv('mkdir', '-p', os.path.join(self.src, rel))
            run_priv('chmod', '0555', os.path.join(self.src, rel))
        deep = os.path.join(self.src, 'app', 'sub')
        run_priv('sh', '-c', 'echo x > "$0/f.txt" && chown 0:0 "$0/f.txt" && chmod 0444 "$0/f.txt"', deep)
        self.own(deep, '0555')
        before = self.snapshot(self.src)
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        failed = [l for l in result.stdout.splitlines() if l.startswith('FAIL')]
        self.assertTrue(any('owned by the agent' in l and deep in l for l in failed), result.stdout)
        self.assertEqual(self.snapshot(self.src), before)
        # as the owner the agent can chmod the directory and then add or replace entries
        script = f"import os; d = {deep!r}; os.chmod(d, 0o755); open(d + '/planted', 'w').write('x'); print('PLANTED')"
        self.assertIn('PLANTED', as_agent(sys.executable, '-I', '-c', script).stdout)

    def test_agent_owned_source_root_is_rejected(self):
        self.own(self.src, '0555')
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('owned by the agent' in l for l in result.stdout.splitlines() if l.startswith('FAIL')))

    def test_entry_owned_by_a_third_uid_is_not_root_owned_and_is_rejected(self):
        victim = os.path.join(self.src, 'README.md')
        run_priv('chown', 'nobody', victim)
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('every source entry is root-owned' in l for l in result.stdout.splitlines() if l.startswith('FAIL')))

    def test_agent_owned_ancestor_with_read_only_mode_is_rejected(self):
        # an owner can chmod its directory, so a 0555 directory it owns on the path is not a boundary
        self.own(self.review, '0555')
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('no ancestor of the source' in l for l in result.stdout.splitlines() if l.startswith('FAIL')), result.stdout)

    def test_supplementary_group_write_access_is_rejected(self):
        victim = os.path.join(self.src, 'app', 'main.py')
        run_priv('chgrp', AGENT, victim)
        run_priv('chmod', '0664', victim)       # root-owned, group-writable by the agent's group
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('write/truncate/append/chmod' in l for l in result.stdout.splitlines() if l.startswith('FAIL')))

    def test_posix_acl_granting_the_agent_write_is_rejected(self):
        if not shutil.which('setfacl'):
            self.skipTest('setfacl is not installed')
        victim = os.path.join(self.src, 'app', 'main.py')
        done = run_priv('setfacl', '-m', f'u:{AGENT}:rw', victim, check=False)
        if done.returncode != 0:
            self.skipTest('filesystem has no ACL support')
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('write/truncate/append/chmod' in l for l in result.stdout.splitlines() if l.startswith('FAIL')))

    def test_hard_linked_source_file_and_symlink_are_rejected(self):
        victim = os.path.join(self.src, 'app', 'main.py')
        run_priv('ln', victim, os.path.join(self.base, 'alias-elsewhere'))
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('hard-linked' in l for l in result.stdout.splitlines()), result.stdout)
        run_priv('rm', '-f', os.path.join(self.base, 'alias-elsewhere'))
        run_priv('ln', '-s', '/etc/passwd', os.path.join(self.src, 'sneaky'))
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(any('(symlink)' in l for l in result.stdout.splitlines()), result.stdout)

    def test_clean_root_owned_tree_still_passes_and_is_unchanged(self):
        before = self.snapshot(self.src)
        result = self.probe(self.src)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('PASS no source entry is owned by the agent', result.stdout)
        self.assertEqual(self.snapshot(self.src), before)

    def test_destructive_probes_refuse_anything_not_marked_disposable(self):
        _impl, work = self.agent_layout()
        before = self.snapshot(work)
        for fn in ('destructive_probe', 'destructive_probe_parent'):
            code = ("import sys; sys.path.insert(0, %r)\nimport fsisolate as f\n"
                    "try:\n  f.%s(%r, [], [%r])\nexcept f.IsolationError as e:\n  print('REFUSED', e)\n"
                    % (self.tool, fn, work, self.home))
            out = as_agent(sys.executable, '-c', code)
            self.assertIn('REFUSED', out.stdout, out.stdout + out.stderr)
        self.assertEqual(self.snapshot(work), before)

    def test_production_cli_cannot_reach_the_destructive_probes(self):
        source = (SCRIPTS / 'opencode_fsisolate.py').read_text()
        cli = source[source.index('def main('):]
        self.assertNotIn('destructive', cli)

    def test_permission_verdict_matches_real_destructive_outcomes_on_a_disposable_fixture(self):
        """The access-based verdict is only trustworthy if it agrees with what the kernel really does."""
        for parent_owner, expect_clean in (('root', True), (AGENT, False)):
            impl = os.path.join(self.base, f'fixture-{parent_owner}')
            fixture = os.path.join(impl, 'work')
            priv_dir(impl, parent_owner)
            priv_dir(fixture, AGENT)
            self.populate(fixture, AGENT)
            run_priv('sh', '-c', f'touch "{impl}/{fsisolate.FIXTURE_MARKER}"')
            predicted = as_agent(sys.executable, '-I', self.tool_script, 'probe-parent', fixture, '--writable', self.home)
            code = ("import sys; sys.path.insert(0, %r)\nimport fsisolate as f\n"
                    "r = f.destructive_probe_parent(%r, [], [%r])\n"
                    "print('BAD' if [x for x in r if not x[1]] else 'CLEAN')\n" % (self.tool, fixture, self.home))
            actual = as_agent(sys.executable, '-c', code)
            self.assertEqual(predicted.returncode == 0, expect_clean, predicted.stdout)
            self.assertEqual('CLEAN' in actual.stdout, expect_clean, actual.stdout + actual.stderr)

    def test_collector_refuses_a_worktree_the_agent_could_swap(self):
        scratch = temp_dir(self, 'ocfs-link-')
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


class Runner:
    """Records every command the helper runs. Optionally forces the remount to fail or everything to fail."""

    def __init__(self, real=False, fail_bind=False, fail_remount=False, fail_umount=False, raises=None):
        self.real, self.fail_bind, self.fail_remount = real, fail_bind, fail_remount
        self.fail_umount, self.raises, self.calls = fail_umount, raises, []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.raises:
            raise self.raises
        refuse = ((cmd[:2] == ['mount', '--bind'] and self.fail_bind)
                  or (cmd[:2] == ['mount', '-o'] and self.fail_remount)
                  or (cmd[0] == 'umount' and self.fail_umount))
        if self.real and not refuse:
            return subprocess.run([*PRIV, *cmd], capture_output=True)
        return subprocess.CompletedProcess(cmd, 1 if refuse else 0)


class MountHelperTests(unittest.TestCase):
    """The production read-only mount helper (fsisolate.mount_readonly) with a recording runner: no real mounts."""

    def setUp(self):
        self.root = os.path.join(temp_dir(self, 'ocmnt-'), 'review')
        self.src = os.path.join(self.root, 'src')
        os.makedirs(self.src)

    def test_bind_failure_creates_no_mount_and_runs_only_the_bind(self):
        runner = Runner(fail_bind=True)
        self.assertFalse(fsisolate.mount_readonly(self.root, run=runner))
        self.assertEqual([c[0] for c in runner.calls], ['mount'])

    def test_missing_privileges_or_mount_binary_is_unavailable_not_an_error(self):
        for error in (PermissionError('denied'), FileNotFoundError('mount')):
            runner = Runner(raises=error)
            self.assertFalse(fsisolate.mount_readonly(self.root, run=runner))
            self.assertEqual(len(runner.calls), 1)

    def test_remount_failure_after_a_bind_removes_the_bind(self):
        runner = Runner(fail_remount=True)
        self.assertFalse(fsisolate.mount_readonly(self.root, run=runner))
        self.assertEqual([c[:2] for c in runner.calls], [['mount', '--bind'], ['mount', '-o'], ['umount', self.src]])

    def test_a_bind_that_cannot_be_removed_is_reported_not_swallowed(self):
        runner = Runner(fail_remount=True, fail_umount=True)
        with self.assertRaises(fsisolate.IsolationError) as caught:
            fsisolate.mount_readonly(self.root, run=runner)
        self.assertIn('could not be removed', str(caught.exception))
        self.assertEqual([c[0] for c in runner.calls], ['mount', 'mount', 'umount', 'umount'])

    def test_cli_reports_a_stuck_partial_mount_with_a_failing_exit_code(self):
        original = fsisolate.mount_readonly
        fsisolate.mount_readonly = lambda root, run=None: original(root, run=Runner(fail_remount=True, fail_umount=True))
        self.addCleanup(setattr, fsisolate, 'mount_readonly', original)
        self.assertEqual(fsisolate.main(['x', 'mount', self.root]), 1)


@unittest.skipUnless(DISPOSABLE, PRIVILEGED_SKIP)
class RealMountTests(unittest.TestCase):
    """Real bind mounts made by the production helper. Every fixture lives in its own root-owned disposable
    directory and is left mounted for the VM teardown; nothing here unmounts or deletes."""

    def setUp(self):
        self.root = os.path.join(disposable_dir('ocmnt-'), 'review')
        self.src = os.path.join(self.root, 'src')
        run_priv('mkdir', '-p', os.path.join(self.src, 'app'))
        run_priv('sh', '-c', 'echo original > "$0/app/main.py"', self.src)
        self.before = RuntimeFilesystemTests.snapshot(self.src)

    def assert_source_unchanged(self):
        self.assertEqual(RuntimeFilesystemTests.snapshot(self.src), self.before, 'source files changed')

    def test_real_bind_and_remount_succeed_once_and_block_writes(self):
        runner = Runner(real=True)
        self.assertTrue(fsisolate.mount_readonly(self.root, run=runner))
        self.assertEqual(len(runner.calls), 2)                                    # exactly one bind, one remount
        self.assertTrue(mounted(self.src))
        write = run_priv('sh', '-c', f'echo x > {self.src}/app/main.py', check=False)   # even root is refused
        self.assertNotEqual(write.returncode, 0)
        self.assertIn('Read-only file system', write.stderr)
        self.assert_source_unchanged()

    def test_real_bind_followed_by_remount_failure_leaves_no_mount(self):
        runner = Runner(real=True, fail_remount=True)
        self.assertFalse(fsisolate.mount_readonly(self.root, run=runner))
        self.assertFalse(mounted(self.src))                                       # the helper itself removed its bind
        self.assertEqual([c[:2] for c in runner.calls], [['mount', '--bind'], ['mount', '-o'], ['umount', self.src]])
        self.assert_source_unchanged()

    def test_real_stuck_partial_mount_is_reported(self):
        runner = Runner(real=True, fail_remount=True, fail_umount=True)
        with self.assertRaises(fsisolate.IsolationError):
            fsisolate.mount_readonly(self.root, run=runner)
        self.assertTrue(mounted(self.src))                                        # left for the VM teardown

    def test_the_cli_mounts_once_and_reports_active(self):
        out = run_priv(sys.executable, '-I', str(SCRIPTS / 'opencode_fsisolate.py'), 'mount', self.root)
        self.assertIn('active', out.stdout)
        self.assertTrue(mounted(self.src))
        self.assertEqual(subprocess.run(['findmnt', '-rn', '--mountpoint', self.src], capture_output=True,
                                        text=True).stdout.count('\n'), 1)


class FsIsolateUnitTests(unittest.TestCase):
    """Unprivileged unit checks of the building blocks of the reviewer filesystem preflight."""

    def test_a_permission_verdict_requires_a_permission_class_error(self):
        denied = fsisolate._denied
        for code in (errno.EACCES, errno.EPERM, errno.EROFS):
            def refuse(code=code):
                raise OSError(code, os.strerror(code))
            self.assertTrue(denied(refuse)[0], os.strerror(code))
        def missing():
            raise OSError(errno.ENOENT, 'no such file')
        self.assertFalse(denied(missing)[0], 'a missing path is not evidence that writing is denied')
        def broken():
            raise OSError(errno.EIO, 'input/output error')
        self.assertFalse(denied(broken)[0])
        ok, detail = denied(lambda: None)
        self.assertFalse(ok)                                        # an operation that succeeded is never "denied"
        self.assertIn('SUCCEEDED', detail)

    def test_the_ownership_chain_rejects_group_and_world_writable_directories(self):
        base = temp_dir(self, 'occhain-')
        middle = os.path.join(base, 'middle')
        leaf = os.path.join(middle, 'leaf')
        os.makedirs(leaf)
        os.chmod(leaf, 0o755)
        os.chmod(middle, 0o755)
        quiet = [p for p in fsisolate.chain_problems(leaf, os.getuid()) if 'writable' in p]
        for mode, who in ((0o777, 'world'), (0o775, 'group'), (0o757, 'world')):
            os.chmod(middle, mode)
            problems = [p for p in fsisolate.chain_problems(leaf, os.getuid()) if 'writable' in p]
            self.assertTrue(any(middle in p for p in problems), f'{who}-writable ancestor {oct(mode)} was accepted')
        os.chmod(middle, 0o755)
        self.assertEqual([p for p in fsisolate.chain_problems(leaf, os.getuid()) if middle in p and 'writable' in p], [])
        self.assertEqual([p for p in quiet if middle in p], [])

    def test_the_ownership_chain_rejects_symlinks_and_non_directories(self):
        base = temp_dir(self, 'occhain-')
        real = os.path.join(base, 'real')
        os.mkdir(real)
        os.symlink(real, os.path.join(base, 'link'))
        Path(base, 'file').write_text('x')
        self.assertTrue(any('symlink' in p for p in fsisolate.chain_problems(os.path.join(base, 'link'), os.getuid())))
        self.assertTrue(any('not a directory' in p for p in fsisolate.chain_problems(os.path.join(base, 'file'), os.getuid())))


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
        self.tmp = temp_dir(self, 'ocgit-')
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
        real_run, commands = subprocess.run, []

        def recording(command, *args, **kwargs):
            commands.append(list(command))
            return real_run(command, *args, **kwargs)

        with unittest.mock.patch.object(publish.subprocess, 'run', recording), self.assertRaises(publish.PushRejected) as caught:
            self.repo.cas('feature', 'feature', self.n)
        self.assertIn('non-SHA', str(caught.exception))               # refused by the validation, not by git failing
        self.assertFalse([c for c in commands if 'push' in c or 'fetch' in c], commands)   # nothing was sent to the remote
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
        self.tmp = temp_dir(self, 'ocpub-')
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
