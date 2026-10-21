"""Behavioural tests for the strict cleanup helpers and the mutation runner's cleanup (tests v1.0).

Every directory here is a throwaway tree the test creates; destructive operations run only against those.
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strict_cleanup as sc  # noqa: E402

mutation_check = importlib.import_module('mutation_check')
runtime = importlib.import_module('test_opencode_runtime')
sandbox_tests = importlib.import_module('test_opencode_sandbox')

PRIV = runtime.PRIV
MOUNTS = runtime.MOUNTS_AVAILABLE


def run_inner(test_body=None, set_up_class=None, patch=None):
    """Run a throwaway TestCase through the real unittest machinery and return its TestResult."""
    class Inner(unittest.TestCase):
        pass

    if set_up_class is not None:
        Inner.setUpClass = classmethod(lambda cls: set_up_class(cls))
    if test_body is not None:
        Inner.test_it = lambda self: test_body(self)
    else:
        Inner.test_it = lambda self: None
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(result)
    return result


def private_tmp(case):
    """Point tempfile at a private directory for this test, so checks for leftovers cannot be confused by
    other processes (the mutation runner runs this suite in parallel) and never touch their temporary trees."""
    private = sc.temp_dir(case, 'ocprivtmp-')
    patcher = unittest.mock.patch.object(tempfile, 'tempdir', private)
    patcher.start()
    case.addCleanup(patcher.stop)
    return private


def leftovers(directory):
    return sorted(os.listdir(directory))


class DirectoryIdentityTests(unittest.TestCase):
    """A path is only deleted if it is still the very directory the test created (st_dev, st_ino)."""

    def make(self):
        path = tempfile.mkdtemp(prefix='ocid-')
        self.addCleanup(lambda: os.path.lexists(path) and shutil.rmtree(path))
        return path, sc.identity(path)

    def test_the_created_directory_and_its_whole_tree_are_removed(self):
        path, ident = self.make()
        os.makedirs(os.path.join(path, 'a', 'b'))
        Path(path, 'a', 'b', 'f.txt').write_text('x')
        Path(path, 'top.txt').write_text('x')
        os.symlink('/nonexistent', os.path.join(path, 'dangling'))
        sc.remove_verified(path, ident)
        self.assertFalse(os.path.lexists(path))

    def test_rename_and_replace_with_an_ordinary_directory_is_refused(self):
        path, ident = self.make()
        Path(path, 'original.txt').write_text('original')
        moved = path + '.moved'
        os.rename(path, moved)
        self.addCleanup(lambda: os.path.lexists(moved) and shutil.rmtree(moved))
        os.mkdir(path)                                             # same pathname, different directory
        Path(path, 'replacement.txt').write_text('replacement')
        with self.assertRaises(sc.CleanupRefused) as caught:
            sc.remove_verified(path, ident)
        self.assertIn('not the directory that was created', str(caught.exception))
        self.assertEqual(Path(path, 'replacement.txt').read_text(), 'replacement')      # replacement untouched
        self.assertEqual(Path(moved, 'original.txt').read_text(), 'original')            # original not mistaken for it

    def test_a_renamed_original_is_not_mistaken_for_a_replacement(self):
        path, ident = self.make()
        moved = path + '.moved'
        os.rename(path, moved)
        self.addCleanup(lambda: os.path.lexists(moved) and shutil.rmtree(moved))
        self.assertEqual(sc.identity(moved), ident)                # the original keeps its identity under a new name
        with self.assertRaises(sc.CleanupRefused):                 # and the old pathname no longer names it
            sc.remove_verified(path, ident)
        sc.remove_verified(moved, ident)                           # the original is still deletable by identity
        self.assertFalse(os.path.lexists(moved))

    def test_a_symlink_swapped_in_is_refused_and_its_target_survives(self):
        path, ident = self.make()
        outside = tempfile.mkdtemp(prefix='ocid-outside-')
        self.addCleanup(shutil.rmtree, outside)
        Path(outside, 'keep.txt').write_text('keep')
        shutil.rmtree(path)
        os.symlink(outside, path)
        with self.assertRaises(sc.CleanupRefused):
            sc.remove_verified(path, ident)
        self.assertEqual(Path(outside, 'keep.txt').read_text(), 'keep')
        os.unlink(path)

    def test_a_swapped_ancestor_is_refused(self):
        parent = tempfile.mkdtemp(prefix='ocid-parent-')
        self.addCleanup(shutil.rmtree, parent, True)
        child = os.path.join(parent, 'child')
        os.mkdir(child)
        ident = sc.identity(child)
        os.rename(parent, parent + '.moved')
        self.addCleanup(shutil.rmtree, parent + '.moved', True)
        os.makedirs(child)                                         # same full path through a brand-new ancestor
        Path(child, 'replacement.txt').write_text('replacement')
        with self.assertRaises(sc.CleanupRefused):
            sc.remove_verified(child, ident)
        self.assertTrue(Path(child, 'replacement.txt').exists())

    def test_symlinks_inside_the_tree_are_unlinked_not_followed(self):
        path, ident = self.make()
        outside = tempfile.mkdtemp(prefix='ocid-outside-')
        self.addCleanup(shutil.rmtree, outside)
        Path(outside, 'keep.txt').write_text('keep')
        os.symlink(outside, os.path.join(path, 'link-to-dir'))
        os.symlink(os.path.join(outside, 'keep.txt'), os.path.join(path, 'link-to-file'))
        sc.remove_verified(path, ident)
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(Path(outside, 'keep.txt').read_text(), 'keep')

    def test_a_mount_point_below_the_tree_stops_the_walk_before_anything_under_it_is_touched(self):
        path, ident = self.make()
        os.makedirs(os.path.join(path, 'mounted', 'inner'))
        Path(path, 'mounted', 'inner', 'data.txt').write_text('data')
        below = os.path.join(sc.canonical(path), 'mounted')
        info = os.stat(path)
        fd = os.open(path, sc._DIR_FLAGS)
        try:
            with self.assertRaises(sc.MountLeak):
                sc._children(fd, sc.canonical(path), info.st_dev, {below})
        finally:
            os.close(fd)
        self.assertEqual(Path(path, 'mounted', 'inner', 'data.txt').read_text(), 'data')

    def test_temp_dir_registers_identity_checked_cleanup_that_surfaces_beside_the_test_failure(self):
        created = []

        def body(case):
            created.append(sc.temp_dir(case, 'ocid-case-'))
            os.rename(created[0], created[0] + '.moved')
            os.mkdir(created[0])
            Path(created[0], 'replacement.txt').write_text('replacement')
            case.fail('verification failed')

        result = run_inner(body)
        self.addCleanup(lambda: [shutil.rmtree(p, True) for p in (created[0], created[0] + '.moved')])
        self.assertEqual(len(result.failures), 1)                      # the original failure is preserved
        self.assertIn('verification failed', result.failures[0][1])
        self.assertEqual(len(result.errors), 1)                        # and the refused cleanup is reported next to it
        self.assertIn('not the directory that was created', result.errors[0][1])
        self.assertTrue(Path(created[0], 'replacement.txt').exists())


@unittest.skipIf(PRIV is None, 'needs root or passwordless sudo')
class PrivilegedIdentityTests(unittest.TestCase):
    def make(self):
        path = sc.subprocess.run([*PRIV, 'mktemp', '-d', os.path.join(tempfile.gettempdir(), 'ocpriv-XXXXXX')], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run([*PRIV, 'chmod', '755', path], check=True)
        self.addCleanup(lambda: subprocess.run([*PRIV, 'rm', '-rf', '--one-file-system', path, path + '.moved'], check=False))
        return path, sc.identity(path)

    def test_privileged_removal_deletes_the_verified_root_owned_tree(self):
        path, ident = self.make()
        subprocess.run([*PRIV, 'sh', '-c', 'mkdir -p "$0/a/b" && echo x > "$0/a/b/f" && chmod 0400 "$0/a/b/f"', path], check=True)
        sc.remove_privileged_tree(path, PRIV, ident)
        self.assertFalse(os.path.lexists(path))

    def test_privileged_cleanup_never_deletes_a_replacement_tree(self):
        path, ident = self.make()
        subprocess.run([*PRIV, 'sh', '-c', 'echo original > "$0/original.txt"', path], check=True)
        subprocess.run([*PRIV, 'mv', path, path + '.moved'], check=True)
        subprocess.run([*PRIV, 'sh', '-c', 'mkdir "$0" && echo replacement > "$0/replacement.txt"', path], check=True)
        with self.assertRaises(sc.CleanupRefused):
            sc.remove_privileged_tree(path, PRIV, ident)
        replacement = subprocess.run([*PRIV, 'cat', os.path.join(path, 'replacement.txt')], capture_output=True, text=True)
        self.assertEqual(replacement.stdout.strip(), 'replacement')
        original = subprocess.run([*PRIV, 'cat', os.path.join(path + '.moved', 'original.txt')], capture_output=True, text=True)
        self.assertEqual(original.stdout.strip(), 'original')

    def test_privileged_cleanup_refuses_a_symlink_and_leaves_its_target_alone(self):
        path, ident = self.make()
        target = tempfile.mkdtemp(prefix='ocpriv-target-')
        self.addCleanup(shutil.rmtree, target)
        Path(target, 'keep.txt').write_text('keep')
        subprocess.run([*PRIV, 'rm', '-rf', path], check=True)
        os.symlink(target, path)
        with self.assertRaises(sc.CleanupRefused):
            sc.remove_privileged_tree(path, PRIV, ident)
        self.assertEqual(Path(target, 'keep.txt').read_text(), 'keep')
        os.unlink(path)

    def test_the_root_helper_itself_enforces_identity(self):
        path, ident = self.make()
        wrong = (ident[0], ident[1] + 1)
        done = subprocess.run([*PRIV, sys.executable, '-I', sc.__file__, 'rm-verified', path, str(wrong[0]), str(wrong[1])],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 3)
        self.assertTrue(os.path.isdir(path))

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_privileged_cleanup_refuses_a_live_mount_and_deletes_nothing_through_it(self):
        path, ident = self.make()
        sub = os.path.join(path, 'sub')
        subprocess.run([*PRIV, 'sh', '-c', 'mkdir -p "$0/sub" && echo keep > "$0/sub/keep.txt"', path], check=True)
        subprocess.run([*PRIV, 'mount', '--bind', sub, sub], check=True)
        self.addCleanup(runtime.unmount_all, sub, 0)
        with self.assertRaises(sc.MountLeak):
            sc.remove_privileged_tree(path, PRIV, ident)
        kept = subprocess.run([*PRIV, 'cat', os.path.join(sub, 'keep.txt')], capture_output=True, text=True)
        self.assertEqual(kept.stdout.strip(), 'keep')

    def test_privileged_temp_dir_registers_verified_removal_and_reports_a_swap_beside_the_failure(self):
        created = []

        def body(case):
            created.append(sc.privileged_temp_dir(case, 'ocpriv-case-', PRIV))
            subprocess.run([*PRIV, 'mv', created[0], created[0] + '.moved'], check=True)
            subprocess.run([*PRIV, 'sh', '-c', 'mkdir "$0" && echo replacement > "$0/r.txt"', created[0]], check=True)
            case.fail('verification failed')

        result = run_inner(body)
        self.addCleanup(lambda: subprocess.run([*PRIV, 'rm', '-rf', '--one-file-system', created[0], created[0] + '.moved'], check=False))
        self.assertEqual((len(result.failures), len(result.errors)), (1, 1))
        self.assertIn('verification failed', result.failures[0][1])
        self.assertIn('not the directory that was created', result.errors[0][1])
        kept = subprocess.run([*PRIV, 'cat', os.path.join(created[0], 'r.txt')], capture_output=True, text=True)
        self.assertEqual(kept.stdout.strip(), 'replacement')


class ClassCleanupTests(unittest.TestCase):
    """setUpClass that raises after creating a directory must still get strict cleanup (tearDownClass would not run)."""

    def test_class_cleanup_runs_when_a_later_setup_step_raises(self):
        made = []

        def set_up(cls):
            made.append(sc.class_temp_dir(cls, 'occls-'))
            raise OSError('chmod failed after the directory was created')

        result = run_inner(set_up_class=set_up)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('chmod failed', result.errors[0][1])
        self.assertFalse(os.path.lexists(made[0]))

    def test_class_cleanup_failure_is_visible_beside_the_setup_error(self):
        made = []

        def set_up(cls):
            made.append(sc.class_temp_dir(cls, 'occls-'))
            os.rename(made[0], made[0] + '.moved')
            os.mkdir(made[0])
            raise OSError('setup failed')

        result = run_inner(set_up_class=set_up)
        self.addCleanup(lambda: [shutil.rmtree(p, True) for p in (made[0], made[0] + '.moved')])
        text = ' '.join(e[1] for e in result.errors)
        self.assertEqual(len(result.errors), 2)
        self.assertIn('setup failed', text)
        self.assertIn('not the directory that was created', text)
        self.assertTrue(os.path.isdir(made[0]))                    # the replacement was not deleted

    def test_class_cleanup_runs_after_a_successful_class(self):
        made = []
        result = run_inner(set_up_class=lambda cls: made.append(sc.class_temp_dir(cls, 'occls-')))
        self.assertTrue(result.wasSuccessful())
        self.assertFalse(os.path.lexists(made[0]))

    def test_the_runtime_tool_directory_is_cleaned_when_its_setup_raises(self):
        private = private_tmp(self)
        runtime_class = runtime.RuntimeFilesystemTests
        result = unittest.TestResult()
        with unittest.mock.patch.object(runtime.shutil, 'copy', side_effect=OSError('copy failed')):
            unittest.defaultTestLoader.loadTestsFromTestCase(runtime_class).run(result)
        self.assertTrue(any('copy failed' in e[1] for e in result.errors), result.errors)
        self.assertEqual(leftovers(private), [])               # the tool directory was created in `private` and removed

    def test_the_proxy_log_directory_is_cleaned_when_proxy_serve_raises(self):
        private = private_tmp(self)
        result = unittest.TestResult()
        with unittest.mock.patch.object(sandbox_tests.proxy, 'serve', side_effect=RuntimeError('serve failed')):
            unittest.defaultTestLoader.loadTestsFromTestCase(sandbox_tests.ProxyBindingTests).run(result)
        self.assertTrue(any('serve failed' in e[1] for e in result.errors), result.errors)
        self.assertEqual(leftovers(private), [])

    def test_the_proxy_class_cleanup_failure_is_reported_beside_the_serve_error(self):
        private = private_tmp(self)
        result = unittest.TestResult()
        refused = unittest.mock.patch.object(sc, 'remove_verified', side_effect=sc.CleanupRefused('refused'))
        with unittest.mock.patch.object(sandbox_tests.proxy, 'serve', side_effect=RuntimeError('serve failed')), refused:
            unittest.defaultTestLoader.loadTestsFromTestCase(sandbox_tests.ProxyBindingTests).run(result)
        text = ' '.join(e[1] for e in result.errors)
        self.assertIn('serve failed', text)
        self.assertIn('refused', text)
        self.assertEqual(len(leftovers(private)), 1)            # the refused directory is reported, not pretended gone
        for leftover in os.listdir(private):
            shutil.rmtree(os.path.join(private, leftover))


class NothingLeftBehindTests(unittest.TestCase):
    def test_a_successful_test_leaves_no_temporary_tree(self):
        made = []
        result = run_inner(lambda case: made.append(sc.temp_dir(case, 'ocnone-')))
        self.assertTrue(result.wasSuccessful())
        self.assertFalse(os.path.lexists(made[0]))

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_the_real_capability_probe_leaves_no_mount_and_no_tree(self):
        private = private_tmp(self)
        self.assertTrue(runtime.can_mount())
        self.assertEqual(leftovers(private), [])
        self.assertEqual([m for m in sc.mount_points() if m.startswith(sc.canonical(private) + '/')], [])


class AnchoredDetachTests(unittest.TestCase):
    """detach_mounts_anchored() is bound to the directory it opened, not to its pathname. Each test replaces
    the directory at a different step and checks that nothing belonging to the replacement is detached."""

    def worktree(self, *subs):
        parent = sc.temp_dir(self, 'ocanch-')
        work = os.path.join(parent, 'work')
        os.mkdir(work)
        for name in subs:
            os.mkdir(os.path.join(work, name))
        return work, sc.identity(work)

    def swap_in_replacement(self, work, *subs, mount=True):
        """Rename the original aside and create a replacement (with its own mounts) at the same pathname."""
        moved = work + '.moved'
        os.rename(work, moved)
        os.mkdir(work)
        for name in subs:
            target = os.path.join(work, name)
            os.mkdir(target)
            Path(target, 'replacement.txt').write_text('replacement')
            if mount:
                subprocess.run([*PRIV, 'mount', '--bind', target, target], check=True)
                self.addCleanup(runtime.unmount_all, target, 0)
        return moved

    def scenario(self, body):
        """Run `body` (Python source) as root in a fresh interpreter and return the JSON it prints.
        Mount operations need root, and they must work the same whether the suite runs as root or through sudo."""
        parent = sc.privileged_temp_dir(self, 'ocanch-', PRIV)
        prelude = ('import json, os, subprocess, sys\n'
                   f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
                   'import strict_cleanup as sc\n'
                   'import mutation_check\n'
                   f'parent = {parent!r}\n'
                   'def sh(*cmd):\n    subprocess.run(list(cmd), check=True)\n')
        done = subprocess.run([*PRIV, sys.executable, '-I', '-c', prelude + body], capture_output=True, text=True)
        self.addCleanup(runtime.unmount_all, parent, 0)
        for leftover in sc.mounts_under(parent):
            self.addCleanup(runtime.unmount_all, leftover, 0)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        import json
        return json.loads(done.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_replacement_created_during_enumeration_keeps_its_mount(self):
        outcome = self.scenario('''
results = {}
for stage in ('opened', 'enumerated', 'detached'):
    work = os.path.join(parent, 'work-' + stage)
    os.mkdir(work)
    for name in ('a', 'b'):
        os.mkdir(os.path.join(work, name))
        sh('mount', '--bind', os.path.join(work, name), os.path.join(work, name))
    ident = sc.identity(work)
    fired = []

    def hook(seen, work=work, stage=stage, fired=fired):
        if seen == stage and not fired:
            moved = work + '.moved'
            os.rename(work, moved)               # the race: the directory is replaced mid-operation
            os.mkdir(work)
            for name in ('a', 'b'):
                target = os.path.join(work, name)
                os.mkdir(target)
                open(os.path.join(target, 'replacement.txt'), 'w').write('replacement')
                sh('mount', '--bind', target, target)
            fired.append(moved)

    detached = sc.detach_mounts_anchored(work, ident, hook=hook)
    moved = fired[0]
    results[stage] = {
        'detached': detached,
        'original_mounts_left': [len(sc.mounts_at(os.path.join(moved, n))) for n in ('a', 'b')],
        'replacement_mounts': [len(sc.mounts_at(os.path.join(work, n))) for n in ('a', 'b')],
        'replacement_files': [open(os.path.join(work, n, 'replacement.txt')).read() for n in ('a', 'b')],
    }
    for n in ('a', 'b'):                          # leave nothing mounted
        for base in (work, moved):
            while sc.mounts_at(os.path.join(base, n)):
                sh('umount', os.path.join(base, n))
print(json.dumps(results))
''')
        for stage, seen in outcome.items():
            self.assertGreaterEqual(seen['detached'], 2, stage)
            self.assertEqual(seen['original_mounts_left'], [0, 0], f'{stage}: the original directory kept a mount')
            self.assertEqual(seen['replacement_mounts'], [1, 1], f'{stage}: the replacement lost a mount')
            self.assertEqual(seen['replacement_files'], ['replacement', 'replacement'], stage)

    def test_every_detach_goes_through_the_descriptor_never_the_old_pathname(self):
        work, ident = self.worktree('a', 'b')
        table = (f'1 2 0:3 / {work}/a rw - ext4 /dev/vda rw\n1 2 0:3 / {work}/b rw - ext4 /dev/vda rw\n')
        seen, calls = [], []

        def hook(stage):
            if stage == 'enumerated' and not seen:
                seen.append(self.swap_in_replacement(work, 'a', mount=False))

        state = {'moved': False}

        def read():
            return table if not state['moved'] else ''

        with unittest.mock.patch.object(sc, 'read_mountinfo', read):
            def umount2(path):
                calls.append(path)
                state['moved'] = True

            # the table still lists the ORIGINAL pathname; after the swap the original lives at '.moved'
            sc.detach_mounts_anchored(work, ident, hook=hook, umount2=umount2)
        self.assertTrue(calls, 'nothing was detached')
        for path in calls:
            self.assertTrue(path.startswith('/proc/self/fd/'), path)       # resolved by the kernel inside the directory
            self.assertNotIn(work + '/', path)

    def test_a_directory_that_is_not_the_one_created_is_refused_before_anything_is_enumerated(self):
        work, ident = self.worktree('a')
        moved = self.swap_in_replacement(work, 'a', mount=False)
        self.addCleanup(shutil.rmtree, moved, True)
        read = unittest.mock.Mock(return_value='')
        with unittest.mock.patch.object(sc, 'read_mountinfo', read), self.assertRaises(sc.CleanupRefused):
            sc.detach_mounts_anchored(work, ident)
        read.assert_not_called()

    def test_a_directory_that_loses_its_location_fails_safe_without_detaching(self):
        work, ident = self.worktree('a')
        calls = []

        def hook(stage):
            if stage == 'opened':
                shutil.rmtree(work)                                        # the anchored directory is deleted

        with self.assertRaises(sc.CleanupRefused) as caught:
            sc.detach_mounts_anchored(work, ident, hook=hook, umount2=calls.append)
        self.assertIn('no longer has a location', str(caught.exception))
        self.assertEqual(calls, [])

    def test_a_directory_that_keeps_moving_fails_safe_without_detaching(self):
        work, ident = self.worktree('a')
        calls = []
        real = os.readlink
        counter = {'n': 0}

        def moving(path, *a, **k):
            if str(path).startswith('/proc/self/fd/'):
                counter['n'] += 1
                return f'/tmp/somewhere-{counter["n"]}'                  # a different answer on every read
            return real(path, *a, **k)

        with unittest.mock.patch.object(sc.os, 'readlink', moving), self.assertRaises(sc.CleanupRefused) as caught:
            sc.detach_mounts_anchored(work, ident, umount2=calls.append)
        self.assertIn('kept moving', str(caught.exception))
        self.assertEqual(calls, [])

    def test_when_proc_is_unavailable_it_fails_instead_of_falling_back_to_the_pathname(self):
        work, ident = self.worktree('a')
        calls = []
        with unittest.mock.patch.object(sc.os, 'readlink', side_effect=OSError('/proc is not mounted')), \
                self.assertRaises(OSError):
            sc.detach_mounts_anchored(work, ident, umount2=calls.append)
        self.assertEqual(calls, [])

    def test_a_mount_that_cannot_be_detached_raises_and_is_not_retried_by_pathname(self):
        work, ident = self.worktree('a')
        table = f'1 2 0:3 / {work}/a rw - ext4 /dev/vda rw\n'
        calls = []

        def failing(path):
            calls.append(path)
            raise OSError(16, 'busy', path)

        with unittest.mock.patch.object(sc, 'read_mountinfo', lambda: table), self.assertRaises(OSError):
            sc.detach_mounts_anchored(work, ident, umount2=failing)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('/proc/self/fd/'))

    @unittest.skipIf(PRIV is None, 'needs root or passwordless sudo')
    def test_the_root_helper_refuses_a_replacement_and_detaches_the_original_when_asked(self):
        work, ident = self.worktree('a')
        wrong = (ident[0], ident[1] + 1)
        done = subprocess.run([*PRIV, sys.executable, '-I', sc.__file__, 'detach-verified', work, str(wrong[0]), str(wrong[1])],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 3)
        with self.assertRaises(sc.CleanupRefused):
            sc.detach_privileged(work, PRIV, wrong)

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_cleanup_worktree_with_a_replacement_swapped_in_during_the_detach_leaves_the_replacement_whole(self):
        outcome = self.scenario('''
from pathlib import Path
work = os.path.join(parent, 'work')
os.makedirs(os.path.join(work, 'a'))
sh('mount', '--bind', os.path.join(work, 'a'), os.path.join(work, 'a'))
ident = sc.identity(work)
moved = []

def racing_detach(path, expected):
    def hook(stage):
        if stage == 'enumerated' and not moved:
            os.rename(work, work + '.moved')
            os.makedirs(os.path.join(work, 'a'))
            open(os.path.join(work, 'a', 'replacement.txt'), 'w').write('replacement')
            sh('mount', '--bind', os.path.join(work, 'a'), os.path.join(work, 'a'))
            moved.append(work + '.moved')
    return sc.detach_mounts_anchored(path, expected, hook=hook)

mutation_check.detach_mounts = racing_detach
result = mutation_check.cleanup_worktree(Path(work), ident)
out = {'state': result['state'], 'detail': result['detail'],
       'replacement_mounts': len(sc.mounts_at(os.path.join(work, 'a'))),
       'replacement_file': open(os.path.join(work, 'a', 'replacement.txt')).read(),
       'original_kept': os.path.isdir(moved[0]),
       'original_mounts': len(sc.mounts_at(os.path.join(moved[0], 'a')))}
while sc.mounts_at(os.path.join(work, 'a')):
    sh('umount', os.path.join(work, 'a'))
print(json.dumps(out))
''')
        self.assertEqual(outcome['state'], 'FAILED')                      # the original was moved; the delete is refused
        self.assertIn('not the directory that was created', outcome['detail'])
        self.assertEqual(outcome['replacement_mounts'], 1)               # the replacement's mount survived
        self.assertEqual(outcome['replacement_file'], 'replacement')
        self.assertTrue(outcome['original_kept'])                        # and the original is left for inspection
        self.assertEqual(outcome['original_mounts'], 0)                  # its own mount was detached through the descriptor


class MutationRunnerCleanupTests(unittest.TestCase):
    """The mutation runner must not report a result while hiding a leaked mount or an undeletable worktree."""

    def worktree(self):
        work = Path(tempfile.mkdtemp(prefix='mut-test-'))
        self.addCleanup(lambda: work.exists() and shutil.rmtree(work, True))
        (work / 'sub').mkdir()
        (work / 'sub' / 'f.txt').write_text('x')
        return work, sc.identity(str(work))

    def test_a_clean_worktree_is_reported_clean_and_removed(self):
        work, ident = self.worktree()
        outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual((outcome['state'], outcome['leaked_mounts']), ('clean', 0))
        self.assertFalse(work.exists())

    def test_an_undeletable_worktree_is_a_failure_not_a_pass(self):
        work, ident = self.worktree()
        with unittest.mock.patch.object(mutation_check, 'delete_tree', side_effect=PermissionError('cannot delete')):
            outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('cannot delete', outcome['detail'])
        self.assertTrue(work.exists())

    def test_a_swapped_worktree_is_refused_and_reported(self):
        work, ident = self.worktree()
        os.rename(work, str(work) + '.moved')
        self.addCleanup(shutil.rmtree, str(work) + '.moved', True)
        work.mkdir()
        (work / 'replacement.txt').write_text('replacement')
        outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('not the directory that was created', outcome['detail'])
        self.assertTrue((work / 'replacement.txt').exists())

    def test_a_leaked_mount_that_cannot_be_detached_is_a_failure_and_nothing_is_deleted(self):
        work, ident = self.worktree()
        deleted = []
        with unittest.mock.patch.object(mutation_check, 'detach_mounts', side_effect=sc.MountLeak('mounts below could not be detached')), \
                unittest.mock.patch.object(mutation_check, 'delete_tree', lambda p, e: deleted.append(p)):
            outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('could not be detached', outcome['detail'])
        self.assertEqual(deleted, [])
        self.assertTrue((work / 'sub' / 'f.txt').exists())

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_a_real_leaked_mount_is_reported_detached_and_the_tree_removed(self):
        work, ident = self.worktree()
        target = str(work / 'sub')
        subprocess.run([*PRIV, 'mount', '--bind', target, target], check=True)
        self.addCleanup(runtime.unmount_all, target, 0)
        outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'reclaimed')            # explicit: the run leaked, the leak is reported
        self.assertGreaterEqual(outcome['leaked_mounts'], 1)
        self.assertFalse(work.exists())

    def test_run_one_reports_cleanup_failed_even_when_the_suite_verdict_is_caught(self):
        item = mutation_check.MUTATIONS[0]
        failing_suite = [sys.executable, '-c', 'import sys; sys.exit(1)']
        parent = sc.temp_dir(self, 'ocmutparent-')
        with unittest.mock.patch.object(mutation_check, 'delete_tree', side_effect=OSError('device busy')):
            result = mutation_check.run_one(item, suite=failing_suite, parent=parent)
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d)) for d in os.listdir(parent)])
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'CAUGHT')
        self.assertIn('device busy', result['detail'])
        summary = mutation_check.summarize([result])
        self.assertEqual(summary['CLEANUP_FAILED'], 1)
        self.assertEqual(mutation_check.exit_code(summary), 1)

    def test_the_mutated_suite_gets_a_private_tmpdir_inside_its_worktree(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        inside = [sys.executable, '-c', 'import os, sys, tempfile; sys.exit(0 if tempfile.gettempdir().startswith(os.getcwd() + os.sep) else 1)']
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=inside, parent=parent)
        self.assertEqual(result['result'], 'MISSED')               # exit 0 means TMPDIR was under the worktree
        self.assertEqual(os.listdir(parent), [])

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_a_mount_leaked_by_the_mutated_suite_is_reported_and_reclaimed_not_left_in_tmp(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        leak = ('import os, subprocess, sys, tempfile\n'
                'd = tempfile.mkdtemp(); t = os.path.join(d, "src"); os.mkdir(t)\n'
                f'subprocess.run({list(PRIV)!r} + ["mount", "--bind", t, t], check=True)\n'
                'sys.exit(1)\n')
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', leak], parent=parent)
        self.assertEqual(result['result'], 'CAUGHT')               # the suite verdict stands...
        self.assertEqual(result['cleanup'], 'reclaimed')           # ...but the leak is explicit in the result
        self.assertGreaterEqual(result['leaked_mounts'], 1)
        self.assertEqual(os.listdir(parent), [])
        self.assertEqual([m for m in sc.mount_points() if m.startswith(sc.canonical(parent) + '/')], [])

    def test_run_one_on_a_clean_run_keeps_the_suite_verdict(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                        parent=parent)
        self.assertEqual((result['result'], result['cleanup']), ('CAUGHT', 'clean'))
        self.assertEqual(os.listdir(parent), [])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 0)


if __name__ == '__main__':
    unittest.main()
