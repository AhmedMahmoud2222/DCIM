"""Behavioural tests for the strict cleanup helpers and the mutation runner's cleanup (tests v1.0).

Every directory here is a throwaway tree the test creates; destructive operations run only against those.
"""
import ctypes
import errno
import importlib
import json
import signal
import time
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

    def test_the_identity_of_a_symlink_is_never_taken_from_its_target(self):
        path, ident = self.make()
        link = path + '.link'
        os.symlink(path, link)
        self.addCleanup(os.unlink, link)
        with self.assertRaises(sc.CleanupRefused):
            sc.identity(link)                                          # a link to a directory is not "the directory"
        self.assertEqual(sc.identity(path), ident)

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


def run_root_scenario(case, body):
    """Run `body` (Python source) as root in a fresh interpreter, in a root-owned scratch directory, and return
    the JSON it prints. Mount operations need root; running them in a helper makes them behave the same whether
    the suite runs as root or through sudo."""
    import json
    parent = sc.privileged_temp_dir(case, 'ocbound-', PRIV)
    prelude = ('import json, os, subprocess, sys\n'
               f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
               'import strict_cleanup as sc\n'
               f'parent = {parent!r}\n'
               'def sh(*cmd):\n    subprocess.run(list(cmd), check=True)\n'
               'def make_victim(name="victim"):\n'
               '    v = os.path.join(parent, name)\n'
               '    os.makedirs(os.path.join(v, "deep"))\n'
               '    open(os.path.join(v, "keep.txt"), "w").write("keep")\n'
               '    open(os.path.join(v, "deep", "keep2.txt"), "w").write("keep2")\n'
               '    return v\n'
               'def snap(v):\n'
               '    out = {}\n'
               '    for base, dirs, files in os.walk(v):\n'
               '        for n in dirs + files:\n'
               '            f = os.path.join(base, n)\n'
               '            i = os.lstat(f)\n'
               '            out[os.path.relpath(f, v)] = [i.st_ino, i.st_mode, open(f).read() if os.path.isfile(f) else None]\n'
               '    return out\n'
               'def unmount_all_below(p):\n'
               '    for _ in range(8):\n'
               '        left = [m for m in sc.mount_points() if m == p or m.startswith(p + "/")]\n'
               '        if not left:\n            return\n'
               '        for m in sorted(left, key=len, reverse=True):\n            subprocess.run(["umount", m])\n')
    done = subprocess.run([*PRIV, sys.executable, '-I', '-c', prelude + body], capture_output=True, text=True)
    case.addCleanup(runtime.unmount_all, parent, 0)
    for leftover in sc.mounts_under(parent):
        case.addCleanup(runtime.unmount_all, leftover, 0)
    case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


# Shared scenario preamble: a worktree with some content and a VICTIM directory on the same filesystem.
TREE = '''
work = os.path.join(parent, "work")
os.makedirs(os.path.join(work, "a", "b"))
os.makedirs(os.path.join(work, "d"))
for rel in ("one.txt", "a/two.txt", "a/b/three.txt", "d/four.txt"):
    open(os.path.join(work, rel), "w").write(rel)
victim = make_victim()
before = snap(victim)
ident = sc.identity(work)
same_fs = os.stat(work).st_dev == os.stat(victim).st_dev
marks = {}
def mark():
    marks["m"] = sorted(sc.mount_points())              # the mount table the cleanup is not allowed to change
def outcome(error=None):
    result = {"error": type(error).__name__ if error else None, "message": str(error) if error else "",
              "victim_intact": snap(victim) == before, "same_fs": same_fs,
              "mount_table_unchanged": sorted(sc.mount_points()) == marks["m"]}
    unmount_all_below(parent)                           # leave nothing mounted for the caller's cleanup
    return result
'''


@unittest.skipIf(PRIV is None, 'needs root or passwordless sudo')
@unittest.skipUnless(sc.boundary_available(), 'openat2(RESOLVE_NO_XDEV) and statx mount ids are unavailable here')
class BoundaryDeleteTests(unittest.TestCase):
    """Recursive deletion is bounded by the kernel (openat2 RESOLVE_NO_XDEV from a directory descriptor, plus a
    statx mount-id check), not by pathnames or st_dev. Every scenario puts a VICTIM directory on the SAME
    filesystem as the worktree, so an st_dev comparison cannot tell them apart, and asserts that the victim is
    intact and that nothing was unmounted."""

    def run_case(self, body):
        return run_root_scenario(self, TREE + body)

    def check(self, result, error, label=''):
        self.assertEqual(result['error'], error, f'{label}: {result["message"]}')
        self.assertTrue(result['same_fs'], f'{label}: the victim must share the worktree filesystem')
        self.assertTrue(result['victim_intact'], f'{label}: something outside the worktree was deleted or changed')
        self.assertTrue(result['mount_table_unchanged'], f'{label}: a mount was detached or created by the cleanup')

    def test_a_same_filesystem_bind_mount_inside_the_tree_is_refused_not_deleted_through(self):
        result = self.run_case('''
sh("mount", "--bind", victim, os.path.join(work, "d"))
mark()
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'MountLeak', 'bind mount of a sibling directory')

    def test_nested_and_stacked_bind_mounts_are_refused(self):
        result = self.run_case('''
sh("mount", "--bind", victim, os.path.join(work, "a", "b"))
sh("mount", "--bind", victim, os.path.join(work, "a", "b"))      # stacked on the same mount point
sh("mount", "--bind", os.path.join(victim, "deep"), os.path.join(work, "d"))
mark()
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'MountLeak', 'nested and stacked mounts')

    def test_a_bind_mounted_file_is_refused(self):
        result = self.run_case('''
sh("mount", "--bind", os.path.join(victim, "keep.txt"), os.path.join(work, "one.txt"))
mark()
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'MountLeak', 'file mount point')

    def test_a_worktree_that_is_itself_a_mount_point_is_refused(self):
        result = self.run_case('''
sh("mount", "--bind", work, work)
ident = sc.identity(work)                                          # the identity of the mounted root
mark()
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'MountLeak', 'worktree is a mount root')

    def test_worktree_renamed_and_its_pathname_replaced_by_a_victim_mount_during_the_walk(self):
        result = self.run_case('''
fired = []
def hook(stage, name):
    if stage == "listed" and not fired:
        os.rename(work, work + ".moved")                           # the race: rename the worktree...
        os.mkdir(work)                                             # ...and put a mount of the victim at its old pathname
        sh("mount", "--bind", victim, work)
        fired.append(1)
        mark()
try:
    sc.remove_verified(work, ident, hook=hook)
    error = None
except Exception as exc:
    error = exc
out = outcome(error)
out["moved_is_empty"] = os.listdir(work + ".moved") == []
print(json.dumps(out))
''')
        self.check(result, 'CleanupRefused', 'worktree renamed and replaced by a mount')
        self.assertTrue(result['moved_is_empty'], 'the original tree should have been emptied through its descriptor')

    def test_ancestor_renamed_and_replaced_by_a_victim_mount_during_the_walk(self):
        result = self.run_case('''
fired = []
def hook(stage, name):
    if stage == "listed" and not fired:
        os.rename(parent + "/work", parent + "/work.moved")
        os.mkdir(parent + "/work")
        sh("mount", "--bind", victim, parent + "/work")
        fired.append(1)
        mark()
try:
    sc.remove_verified(work, ident, hook=hook)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'CleanupRefused', 'pathname replaced by a mount')

    def test_a_descendant_replaced_by_a_victim_mount_after_listing_and_before_opening(self):
        result = self.run_case('''
fired = []
def hook(stage, name):
    if stage == "before-open" and name == "d" and not fired:
        os.rename(os.path.join(work, "d"), os.path.join(work, "d.moved"))
        os.mkdir(os.path.join(work, "d"))
        sh("mount", "--bind", victim, os.path.join(work, "d"))
        fired.append(1)
        mark()
try:
    sc.remove_verified(work, ident, hook=hook)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'MountLeak', 'descendant replaced by a mount')

    def test_a_descendant_replaced_by_a_symlink_to_the_victim_is_never_followed(self):
        result = self.run_case('''
fired = []
def hook(stage, name):
    if stage == "before-open" and name == "d" and not fired:
        os.rename(os.path.join(work, "d"), os.path.join(work, "d.moved"))
        os.symlink(victim, os.path.join(work, "d"))
        fired.append(1)
        mark()
try:
    sc.remove_verified(work, ident, hook=hook)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.assertIsNotNone(result['error'], result['message'])          # the symlink is refused (ELOOP), not followed
        self.check(result, result['error'], 'descendant replaced by a symlink')

    def test_a_descendant_replaced_by_another_ordinary_directory_is_refused(self):
        result = self.run_case('''
fired = []
def hook(stage, name):
    if stage == "before-open" and name == "d" and not fired:
        os.rename(os.path.join(work, "d"), os.path.join(work, "d.moved"))
        os.mkdir(os.path.join(work, "d"))
        fired.append(1)
        mark()
try:
    sc.remove_verified(work, ident, hook=hook)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
        self.check(result, 'CleanupRefused', 'descendant replaced by a directory')

    def test_the_mount_id_check_is_a_second_independent_layer(self):
        for label, mount in (('directory', 'sh("mount", "--bind", victim, os.path.join(work, "d"))'),
                             ('file', 'sh("mount", "--bind", os.path.join(victim, "keep.txt"), os.path.join(work, "one.txt"))')):
            result = self.run_case(mount + '''
mark()
sc._RESOLVE = 0x02 | 0x04                                           # RESOLVE_NO_XDEV off: only the mount-id check is left
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
            self.check(result, 'MountLeak', f'mount id check alone ({label})')

    def test_the_directory_mount_id_check_alone_protects_a_victim_made_only_of_empty_directories(self):
        # Files have their own mount-id check, which would mask a missing directory check on a victim that holds
        # files. An all-directory victim isolates the directory check.
        result = self.run_case('''
empty = os.path.join(parent, "empty-victim")
os.makedirs(os.path.join(empty, "e1", "e2"))
empty_before = snap(empty)
sh("mount", "--bind", empty, os.path.join(work, "d"))
mark()
sc._RESOLVE = 0x02 | 0x04
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
out = outcome(error)
out["empty_victim_intact"] = snap(empty) == empty_before and os.path.isdir(os.path.join(empty, "e1", "e2"))
print(json.dumps(out))
''')
        self.check(result, 'MountLeak', 'directory-only victim')
        self.assertTrue(result['empty_victim_intact'], 'the empty directories of the victim were removed')

    def test_resolve_no_xdev_is_an_independent_first_layer(self):
        for label, mount in (('directory', 'sh("mount", "--bind", victim, os.path.join(work, "d"))'),
                             ('file', 'sh("mount", "--bind", os.path.join(victim, "keep.txt"), os.path.join(work, "one.txt"))')):
            result = self.run_case(mount + '''
mark()
sc.mount_id = lambda fd: 1                                          # every mount "looks the same": only RESOLVE_NO_XDEV is left
try:
    sc.remove_verified(work, ident)
    error = None
except Exception as exc:
    error = exc
print(json.dumps(outcome(error)))
''')
            self.check(result, 'MountLeak', f'RESOLVE_NO_XDEV alone ({label})')

    def test_a_clean_tree_with_nested_content_is_deleted_completely(self):
        result = self.run_case('''
os.symlink(victim, os.path.join(work, "link-to-victim"))
mark()
sc.remove_verified(work, ident)
out = outcome(None)
out["work_gone"] = not os.path.lexists(work)
print(json.dumps(out))
''')
        self.check(result, None, 'clean tree')
        self.assertTrue(result['work_gone'])

    def test_the_boundary_checks_mount_ids_from_statx(self):
        result = self.run_case('''
sh("mount", "--bind", victim, os.path.join(work, "d"))
fd_work = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
fd_d = os.open(os.path.join(work, "d"), os.O_RDONLY | os.O_DIRECTORY)
fd_a = os.open(os.path.join(work, "a"), os.O_RDONLY | os.O_DIRECTORY)
print(json.dumps({"same_dev": os.fstat(fd_work).st_dev == os.fstat(fd_d).st_dev,
                  "mount_ids_differ": sc.mount_id(fd_work) != sc.mount_id(fd_d),
                  "same_mount_same_id": sc.mount_id(fd_work) == sc.mount_id(fd_a)}))
''')
        self.assertEqual(result, {'same_dev': True, 'mount_ids_differ': True, 'same_mount_same_id': True})


class UnsupportedBoundaryTests(unittest.TestCase):
    """If the kernel facility that makes deletion safe is missing or forbidden, deletion is refused: nothing is
    removed and there is no pathname fallback."""

    def tree(self):
        path = sc.temp_dir(self, 'ocunsup-')
        work = os.path.join(path, 'work')
        os.makedirs(os.path.join(work, 'sub'))
        Path(work, 'sub', 'file.txt').write_text('keep')
        return work, sc.identity(work)

    def assert_refused_untouched(self, work, ident, patcher):
        removed = []
        with patcher, unittest.mock.patch.object(os, 'unlink', side_effect=lambda *a, **k: removed.append(a)), \
                unittest.mock.patch.object(os, 'rmdir', side_effect=lambda *a, **k: removed.append(a)), \
                unittest.mock.patch.object(shutil, 'rmtree', side_effect=lambda *a, **k: removed.append(a)):
            with self.assertRaises(sc.BoundaryUnavailable):
                sc.remove_verified(work, ident)
        self.assertEqual(removed, [], 'something was removed although the boundary is unavailable')
        self.assertEqual(Path(work, 'sub', 'file.txt').read_text(), 'keep')

    def fake_libc(self, *, openat2_errno=None, statx=True, statx_mask=0x1000, statx_fails=False):
        calls = []

        class Libc:
            def syscall(self, number, *args):
                calls.append(number)
                if openat2_errno is not None:
                    ctypes.set_errno(openat2_errno)
                    return -1
                return real.syscall(number, *args)

            def __getattr__(self, name):
                if name == 'statx':
                    if not statx:
                        raise AttributeError(name)
                    return self.statx_impl
                return getattr(real, name)

            def statx_impl(self, *args):
                if statx_fails:
                    ctypes.set_errno(errno.ENOSYS)
                    return -1
                buffer = args[4]._obj
                result = real.statx(*args)
                buffer.mask = statx_mask
                return result

        real = ctypes.CDLL(None, use_errno=True)
        return Libc()

    def test_openat2_missing_forbidden_or_unsupported_refuses_deletion(self):
        for code in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):
            work, ident = self.tree()
            self.assert_refused_untouched(work, ident, unittest.mock.patch.object(
                sc, '_libc', lambda code=code: self.fake_libc(openat2_errno=code)))

    def test_statx_missing_failing_or_without_mount_ids_refuses_deletion(self):
        for kwargs in ({'statx': False}, {'statx_fails': True}, {'statx_mask': 0}):
            work, ident = self.tree()
            self.assert_refused_untouched(work, ident, unittest.mock.patch.object(
                sc, '_libc', lambda kwargs=kwargs: self.fake_libc(**kwargs)))

    def test_an_unwired_architecture_refuses_deletion(self):
        work, ident = self.tree()
        self.assert_refused_untouched(work, ident, unittest.mock.patch('platform.machine', return_value='s390x'))

    def test_boundary_available_reports_false_when_a_primitive_fails(self):
        with unittest.mock.patch.object(sc, '_libc', lambda: self.fake_libc(openat2_errno=errno.ENOSYS)):
            self.assertFalse(sc.boundary_available())

    def test_the_privileged_helper_maps_an_unavailable_boundary_to_the_same_error_without_a_fallback(self):
        work, ident = self.tree()
        failed = subprocess.CompletedProcess([], 6, '', 'openat2 is unavailable here; refusing to delete')
        with unittest.mock.patch.object(sc.subprocess, 'run', return_value=failed), \
                self.assertRaises(sc.BoundaryUnavailable) as caught:
            sc.remove_privileged_tree(work, [], ident)
        self.assertIn('refusing to delete', str(caught.exception))
        self.assertEqual(Path(work, 'sub', 'file.txt').read_text(), 'keep')

    def test_the_privileged_helper_result_is_not_ignored(self):
        work, ident = self.tree()
        for completed, expected in ((subprocess.CompletedProcess([], 5, '', 'EBUSY'), OSError),
                                    (subprocess.CompletedProcess([], 4, '', 'mounted'), sc.MountLeak),
                                    (subprocess.CompletedProcess([], 3, '', 'identity'), sc.CleanupRefused),
                                    (subprocess.CompletedProcess([], 1, '', 'crashed'), OSError),
                                    (subprocess.CompletedProcess([], 0, '', ''), OSError)):   # "success" but the tree is still there
            with unittest.mock.patch.object(sc.subprocess, 'run', return_value=completed), self.assertRaises(expected):
                sc.remove_privileged_tree(work, [], ident)
            self.assertTrue(os.path.isdir(work))
        gone = subprocess.CompletedProcess([], 0, '', '')
        real_exists = os.path.lexists
        with unittest.mock.patch.object(sc.subprocess, 'run', return_value=gone), \
                unittest.mock.patch.object(sc.os.path, 'lexists', lambda p: False if p == work else real_exists(p)):
            sc.remove_privileged_tree(work, [], ident)                       # success plus a vanished tree is accepted

    def test_a_tree_that_needs_the_boundary_is_never_deleted_by_the_cli_helper_when_unsupported(self):
        work, ident = self.tree()
        code = ('import sys, errno, ctypes\n'
                f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
                'import strict_cleanup as sc\n'
                'class L:\n'
                '    def syscall(self, *a):\n        ctypes.set_errno(errno.ENOSYS); return -1\n'
                'sc._libc = lambda: L()\n'
                f'sys.exit(sc._main(["x", "rm-verified", {work!r}, "{ident[0]}", "{ident[1]}"]))\n')
        done = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
        self.assertEqual(done.returncode, 6, done.stderr)
        self.assertEqual(Path(work, 'sub', 'file.txt').read_text(), 'keep')


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

    def test_a_mount_visible_in_the_host_namespace_is_a_failure_and_nothing_is_unmounted_or_deleted(self):
        work, ident = self.worktree()
        table = f'1 2 0:3 / {work}/sub rw - ext4 /dev/vda rw\n'
        deleted, ran = [], []
        with unittest.mock.patch.object(sc, 'read_mountinfo', lambda: table), \
                unittest.mock.patch.object(mutation_check, 'delete_tree', lambda p, e: deleted.append(p)), \
                unittest.mock.patch.object(subprocess, 'run', lambda *a, **k: ran.append(a)):
            outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('host namespace', outcome['detail'])
        self.assertEqual((deleted, ran), ([], []))
        self.assertTrue((work / 'sub' / 'f.txt').exists())

    def test_the_runner_contains_no_code_that_unmounts_anything(self):
        # The previous design enumerated mounts and detached them by path; that class of race is gone because the
        # runner never unmounts anything. This guards against it coming back (function bodies only: the mutation
        # inventory quotes old snippets as strings).
        import ast
        forbidden_names = {'umount2', 'detach_mounts', 'detach_privileged', 'detach_mounts_anchored', '_umount2'}
        for name in ('mutation_check.py', 'strict_cleanup.py', 'ns_run.py'):
            tree = ast.parse((Path(__file__).resolve().parent / name).read_text())
            for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(function):
                    used = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
                    self.assertNotIn(used, forbidden_names, f'{name}:{function.name} references {used}')
                    if isinstance(node, ast.Call) and node.args:
                        first = node.args[0]
                        if isinstance(first, (ast.List, ast.Tuple)):
                            literals = [e.value for e in first.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                            self.assertNotIn('umount', literals, f'{name}:{function.name} runs umount')
            self.assertNotIn('MNT_DETACH', [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)], name)


ISOLATION = mutation_check.isolation_available()


@unittest.skipUnless(ISOLATION, 'private PID/mount namespaces and the openat2 boundary are unavailable here')
class NamespaceIsolationTests(unittest.TestCase):
    """A mutated suite runs as PID 1 of private PID and mount namespaces, so nothing it mounts is visible on the
    host, nothing it leaves running survives it, and the worktree is deleted only when the suite is gone."""

    def parent(self):
        return sc.temp_dir(self, 'ocmutparent-')

    def test_the_mutated_suite_gets_a_private_tmpdir_inside_its_worktree(self):
        parent = self.parent()
        inside = [sys.executable, '-c', 'import os, sys, tempfile; sys.exit(0 if tempfile.gettempdir().startswith(os.getcwd() + os.sep) else 1)']
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=inside, parent=parent)
        self.assertEqual(result['result'], 'MISSED')               # exit 0 means TMPDIR was under the worktree
        self.assertEqual(os.listdir(parent), [])

    def test_the_copy_holds_everything_the_suite_reads_so_the_control_can_pass(self):
        parent = self.parent()
        needs = [sys.executable, '-c', 'import os, sys; sys.exit(0 if all(os.path.exists(p) for p in '
                 '("scripts/deploy-docker-compose.sh", "docker-compose.yml", "docs", ".github/workflows", "tests/deployment")) else 1)']
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=needs, parent=parent)
        self.assertEqual(result['result'], 'MISSED', 'the isolated copy is missing a path that the tests read')

    def test_a_mount_made_by_the_suite_is_never_visible_on_the_host_and_is_reported(self):
        if PRIV is None:
            self.skipTest('needs root or passwordless sudo')
        import threading
        parent = self.parent()
        flags = sc.temp_dir(self, 'ocmutflags-')
        mounted, go = os.path.join(flags, 'mounted'), os.path.join(flags, 'go')
        suite = ('import os, subprocess, sys, tempfile, time\n'
                 'd = tempfile.mkdtemp(); t = os.path.join(d, "src"); os.mkdir(t)\n'
                 f'subprocess.run({list(PRIV)!r} + ["mount", "--bind", t, t], check=True)\n'
                 f'open({mounted!r}, "w").write(t)\n'
                 f'deadline = time.time() + 60\n'
                 f'while not os.path.exists({go!r}) and time.time() < deadline:\n    time.sleep(0.05)\n'
                 'sys.exit(1)\n')
        holder = {}
        worker = threading.Thread(target=lambda: holder.update(
            result=mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', suite], parent=parent)))
        worker.start()
        for _ in range(1200):
            if os.path.exists(mounted):
                break
            time.sleep(0.05)
        else:
            Path(go).write_text('x')
            worker.join(60)
            self.fail('the mutated suite never reported its mount')
        seen_on_host = [m for m in sc.mount_points() if m.startswith(sc.canonical(parent) + '/')]
        Path(go).write_text('x')
        worker.join(120)
        self.assertEqual(seen_on_host, [], 'the suite\'s mount propagated to the host namespace')
        result = holder['result']
        self.assertEqual(result['result'], 'CAUGHT')
        self.assertGreaterEqual(result['leaked_mounts'], 1)        # reported from inside the namespace
        self.assertEqual(result['cleanup'], 'reclaimed')
        self.assertEqual(os.listdir(parent), [])

    def test_processes_the_suite_leaves_running_are_killed_before_the_worktree_is_deleted(self):
        parent = self.parent()
        flags = sc.temp_dir(self, 'ocmutflags-')
        beat = os.path.join(flags, 'beat')
        suite = ('import subprocess, sys\n'
                 f'code = "import time\\nwhile True:\\n    open({beat!r}, \'w\').write(str(time.time()))\\n    time.sleep(0.1)\\n"\n'
                 'subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                 'import time; time.sleep(1.0)\n'
                 'sys.exit(1)\n')
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', suite], parent=parent)
        self.assertEqual(result['result'], 'CAUGHT')
        self.assertTrue(os.path.exists(beat), 'the surviving child never ran')
        os.unlink(beat)
        time.sleep(1.0)
        self.assertFalse(os.path.exists(beat), 'a process of the mutated suite survived it')
        self.assertEqual(os.listdir(parent), [])

    def test_a_missing_or_unusable_report_is_an_error_whatever_the_exit_status(self):
        parent = self.parent()
        for status in (0, 1):
            with unittest.mock.patch.object(mutation_check, 'isolated_command', lambda *a, status=status: [sys.executable, '-c', f'import sys; sys.exit({status})']):
                result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
            self.assertEqual(result['result'], 'ERROR', f'exit status {status}')
            self.assertIn('no usable report', result['detail'])
            self.assertEqual(os.listdir(parent), [])

    def test_the_wrapper_refuses_to_run_in_a_namespace_whose_root_is_not_private(self):
        ns_run = importlib.import_module('ns_run')
        self.assertTrue(ns_run.root_is_private('1 2 0:3 / / rw - ext4 /dev/vda rw\n'))
        self.assertFalse(ns_run.root_is_private('1 2 0:3 / / rw shared:1 - ext4 /dev/vda rw\n'))
        self.assertFalse(ns_run.root_is_private('1 2 0:3 / / rw master:1 - ext4 /dev/vda rw\n'))
        self.assertFalse(ns_run.root_is_private('1 2 0:3 / / rw unbindable - ext4 /dev/vda rw\n'))
        self.assertFalse(ns_run.root_is_private(''))                      # no root entry at all: refuse
        report = os.path.join(sc.temp_dir(self, 'ocmutflags-'), 'report.json')
        with unittest.mock.patch.object(sc, 'read_mountinfo', lambda: '1 2 0:3 / / rw shared:1 - ext4 /dev/vda rw\n'):
            code = ns_run.main(['ns_run.py', report, os.getcwd(), '5', '--', sys.executable, '-c', 'pass'])
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(report))                          # the suite was not run

    def test_every_isolated_run_gets_its_own_test_account_name(self):
        parent = self.parent()
        flags = sc.temp_dir(self, 'ocmutflags-')
        names = []
        for index in range(2):
            out = os.path.join(flags, f'name{index}')
            suite = [sys.executable, '-c', f'import os; open({out!r}, "w").write(os.environ["OPENCODE_TEST_ACCOUNT"])']
            mutation_check.run_one(mutation_check.MUTATIONS[0], suite=suite, parent=parent)
            names.append(Path(out).read_text())
        self.assertTrue(all(n.startswith('ocm') and n != 'ocprobe' for n in names), names)
        self.assertNotEqual(names[0], names[1])

    @unittest.skipIf(PRIV is None, 'needs root or passwordless sudo to create an account')
    def test_an_account_the_suite_leaves_behind_is_reported_and_removed(self):
        parent = self.parent()
        suite = [sys.executable, '-c', 'import os, subprocess, sys\n'
                 f'subprocess.run({list(PRIV)!r} + ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", os.environ["OPENCODE_TEST_ACCOUNT"]], check=True)\n'
                 'sys.exit(1)']
        before = subprocess.run(['getent', 'passwd'], capture_output=True, text=True).stdout.splitlines()
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=suite, parent=parent)
        after = subprocess.run(['getent', 'passwd'], capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(result['result'], 'CAUGHT')
        self.assertTrue(result['account_left_behind'])
        self.assertEqual(sorted(after), sorted(before), 'the leaked account is still in the account database')

    def test_an_account_that_cannot_be_removed_is_a_cleanup_failure(self):
        parent = self.parent()
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d), True) for d in os.listdir(parent)])
        with unittest.mock.patch.object(mutation_check, 'reap_account', lambda name: (True, f'account {name} could not be removed: busy')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                            parent=parent)
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'CAUGHT')
        self.assertIn('could not be removed', result['detail'])

    def test_unavailable_isolation_starts_nothing_and_fails_the_run(self):
        parent = self.parent()
        sentinel = os.path.join(sc.temp_dir(self, 'ocmutflags-'), 'ran')
        suite = [sys.executable, '-c', f'open({sentinel!r}, "w").write("ran")']
        with unittest.mock.patch.object(mutation_check, 'isolation_available', lambda: False):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=suite, parent=parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertIn('was not started', result['detail'])
        self.assertFalse(os.path.exists(sentinel), 'the suite ran without isolation')
        self.assertEqual(os.listdir(parent), [])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def test_an_unavailable_deletion_boundary_is_a_cleanup_failure_not_a_pass(self):
        parent = self.parent()
        with unittest.mock.patch.object(mutation_check, 'delete_tree', side_effect=sc.BoundaryUnavailable('openat2 is unavailable')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                            parent=parent)
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d)) for d in os.listdir(parent)])
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'CAUGHT')
        self.assertIn('openat2 is unavailable', result['detail'])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def test_run_one_reports_cleanup_failed_even_when_the_suite_verdict_is_caught(self):
        parent = self.parent()
        with unittest.mock.patch.object(mutation_check, 'delete_tree', side_effect=OSError('device busy')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                            parent=parent)
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d)) for d in os.listdir(parent)])
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'CAUGHT')
        self.assertIn('device busy', result['detail'])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def test_run_one_on_a_clean_run_keeps_the_suite_verdict(self):
        parent = self.parent()
        result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                        parent=parent)
        self.assertEqual((result['result'], result['cleanup']), ('CAUGHT', 'clean'))
        self.assertEqual(os.listdir(parent), [])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 0)

    def test_the_control_run_must_pass_before_any_mutation_is_judged(self):
        parent = self.parent()
        sentinel = os.path.join(sc.temp_dir(self, 'ocmutflags-'), 'mutation-ran')
        failing_control = [sys.executable, '-c', 'import sys; sys.exit(1)']
        control, results = mutation_check.run_all(mutation_check.MUTATIONS[:2], workers=1, suite=failing_control, parent=parent)
        self.assertEqual(control['baseline'], 'FAILED')
        self.assertEqual(results, [])                               # no verdict is produced from a failing control
        self.assertEqual(os.listdir(parent), [])
        self.assertFalse(os.path.exists(sentinel))

    def test_a_passing_control_lets_the_mutations_run_and_a_mutation_that_changes_nothing_is_missed(self):
        parent = self.parent()
        control, results = mutation_check.run_all(mutation_check.MUTATIONS[:2], workers=2, suite=[sys.executable, '-c', 'pass'],
                                                  parent=parent)
        self.assertEqual(control['baseline'], 'PASSED')
        self.assertEqual([r['result'] for r in results], ['MISSED', 'MISSED'])   # a suite that never fails catches nothing
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize(results)), 1)
        self.assertEqual(os.listdir(parent), [])

    def test_main_refuses_to_report_verdicts_when_the_control_fails(self):
        with unittest.mock.patch.object(mutation_check, 'run_all', lambda *a, **k: (
                {'baseline': 'FAILED', 'cleanup': 'clean', 'tests_run': 3, 'first_failure': 'ERROR: test_x'}, [])), \
                unittest.mock.patch.object(sys, 'argv', ['mutation_check.py', '--only', 'P1-1a']), \
                unittest.mock.patch('builtins.print') as printed:
            code = mutation_check.main()
        self.assertEqual(code, 2)
        self.assertTrue(any('no verdict is valid' in str(call) for call in printed.call_args_list))


if __name__ == '__main__':
    unittest.main()


def _spawn(code, *args, cwd='/', env=None, **kw):
    """A throwaway process the test controls; the caller kills it through the returned handle's cleanup."""
    return subprocess.Popen([sys.executable, '-c', code, *args], cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, start_new_session=True, **kw)


class ProcessScanTests(unittest.TestCase):
    """strict_cleanup.processes_using is the positive evidence that nothing of a suite is left: each way a
    process can still touch a worktree is detected on its own, and the absence of all of them is an empty list."""

    def holder(self, code, *args, cwd='/', env=None):
        process = _spawn(code, *args, cwd=cwd, env=env)
        self.addCleanup(self.release, process)
        return process

    @staticmethod
    def release(process):
        mutation_check._kill_group(process.pid)
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def ready(self, process):
        self.assertEqual(process.stdout.readline().strip(), 'ready')

    def scan(self, path, markers=()):
        """The production path: the scan runs as root through the privilege prefix, so it can read every process."""
        if PRIV is None:
            self.skipTest('needs root or passwordless sudo to read every process')
        done = subprocess.run([*PRIV, sys.executable, '-I', sc.__file__, 'procs-using', path, *markers], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return [tuple(line.split('\t', 1)) for line in done.stdout.splitlines()]

    def reasons(self, path, markers=()):
        return [why for pid, why in self.scan(path, markers)]

    def test_nothing_is_reported_when_nothing_holds_the_path(self):
        self.assertEqual(self.scan(sc.temp_dir(self, 'ocscan-')), [])

    def test_a_process_whose_only_link_is_its_working_directory_is_found_and_then_gone(self):
        directory = sc.temp_dir(self, 'ocscan-')
        process = self.holder('import time\nprint("ready", flush=True)\ntime.sleep(60)', cwd=directory)
        self.ready(process)
        self.assertTrue(any(why.startswith('cwd ') for why in self.reasons(directory)))
        mutation_check._kill_group(process.pid)
        process.wait()
        self.assertEqual(self.scan(directory), [])

    def test_a_process_whose_only_link_is_an_open_descriptor_is_found(self):
        directory = sc.temp_dir(self, 'ocscan-')
        target = os.path.join(directory, 'held')
        Path(target).write_text('x')
        process = self.holder('import sys, time\nf = open(sys.stdin.readline().strip())\nprint("ready", flush=True)\ntime.sleep(60)')
        process.stdin.write(target + '\n')
        process.stdin.flush()
        self.ready(process)
        self.assertTrue(any(why.startswith('fd ') for why in self.reasons(directory)), self.reasons(directory))

    def test_a_process_known_only_by_the_run_marker_is_found(self):
        directory = sc.temp_dir(self, 'ocscan-')
        marker = os.path.join(sc.temp_dir(self, 'ocscan-'), 'run-marker')
        process = self.holder('import sys, time\nprint("ready", flush=True)\ntime.sleep(60)', marker)
        self.ready(process)
        self.assertEqual(self.reasons(directory), [])
        self.assertTrue(any('run marker' in why for why in self.reasons(directory, (marker,))))

    def test_a_process_that_exited_leaves_nothing_behind_to_report(self):
        directory = sc.temp_dir(self, 'ocscan-')
        process = _spawn('import sys', cwd=directory)
        process.wait()                                          # reaped: nothing remains, not even a zombie
        process.stdin.close()
        process.stdout.close()
        self.assertEqual(self.scan(directory), [])

    def test_the_scan_reports_an_unreadable_process_as_unverifiable_never_as_gone(self):
        directory = sc.temp_dir(self, 'ocscan-')
        real = open

        def deny(path, *a, **k):
            if str(path).endswith('/maps'):
                raise PermissionError(13, 'denied', str(path))
            return real(path, *a, **k)
        process = self.holder('import time\nprint("ready", flush=True)\ntime.sleep(60)')
        self.ready(process)
        with unittest.mock.patch('builtins.open', deny):
            found = dict(sc.processes_using(directory))
        self.assertTrue(any(why.startswith('unverifiable') for why in found.values()))

    @unittest.skipIf(PRIV is None, 'needs root or passwordless sudo for a mount namespace')
    def test_a_process_whose_only_link_is_a_mount_namespace_holding_a_mount_below_the_path_is_found(self):
        directory = sc.temp_dir(self, 'ocscan-')
        target = os.path.join(directory, 'src')
        os.mkdir(target)
        process = subprocess.Popen([*PRIV, 'unshare', '--mount', '--propagation', 'private', 'sh', '-c',
                                    'read t; mount --bind "$t" "$t"; echo ready; exec sleep 60'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.release, process)
        process.stdin.write(target + '\n')
        process.stdin.flush()
        self.ready(process)
        scan = subprocess.run([*PRIV, sys.executable, '-I', sc.__file__, 'procs-using', directory], capture_output=True, text=True)
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertIn('mount namespace holds a mount', scan.stdout)
        self.assertEqual(sc.mounts_under(directory), [])             # the host itself never saw the mount

    def test_the_command_line_helper_lists_pid_and_reason(self):
        directory = sc.temp_dir(self, 'ocscan-')
        process = self.holder('import time\nprint("ready", flush=True)\ntime.sleep(60)', cwd=directory)
        self.ready(process)
        scan = subprocess.run([sys.executable, '-I', sc.__file__, 'procs-using', directory], capture_output=True, text=True)
        self.assertEqual(scan.returncode, 0)
        self.assertIn(f'{process.pid}\tcwd ', scan.stdout)


BOOT = ('import sys\nsys.path.insert(0, {here!r})\nimport strict_cleanup, ns_run\n'
        'strict_cleanup.read_mountinfo = lambda: "1 2 0:3 / / rw - ext4 /dev/vda rw\\n"\n'
        'sys.exit(ns_run.main(["ns_run.py", {report!r}, {cwd!r}, {timeout!r}, "--", *{command!r}]))\n')


class WrapperLifecycleTests(unittest.TestCase):
    """ns_run reaps the suite's whole process group on every exit path (the namespace then kills the rest)."""

    def setUp(self):
        self.flags = sc.temp_dir(self, 'ocwrap-')
        self.beat, self.pids = os.path.join(self.flags, 'beat'), os.path.join(self.flags, 'pid')
        self.report = os.path.join(self.flags, 'report.json')
        code = (f'import os, time\nopen({self.pids!r}, "w").write(str(os.getpid()))\n'
                f'while True:\n    open({self.beat!r}, "w").write(str(time.time()))\n    time.sleep(0.1)\n')
        self.suite = ('import subprocess, sys, time\n'
                      f'subprocess.Popen([sys.executable, "-c", {code!r}])\n'          # same process group as the suite
                      'time.sleep(60)\n')
        self.addCleanup(self.reap)

    def reap(self):
        if os.path.exists(self.pids):
            try:
                os.kill(int(Path(self.pids).read_text()), 9)
            except (OSError, ValueError):
                pass

    def start(self, timeout):
        boot = BOOT.format(here=os.path.dirname(os.path.abspath(sc.__file__)), report=self.report, cwd=self.flags,
                           timeout=str(timeout), command=[sys.executable, '-c', self.suite])
        return subprocess.Popen([sys.executable, '-c', boot])

    def wait_for_beat(self):
        for _ in range(100):
            if os.path.exists(self.beat):
                return
            time.sleep(0.1)
        self.fail('the descendant never started')

    def assert_descendant_dead(self):
        os.unlink(self.beat)
        time.sleep(0.6)
        self.assertFalse(os.path.exists(self.beat), 'the suite\'s descendant is still running')

    def test_a_suite_timeout_kills_the_whole_process_group_not_just_the_direct_child(self):
        wrapper = self.start(2)
        self.wait_for_beat()
        self.assertEqual(wrapper.wait(30), 1)
        report = json.loads(Path(self.report).read_text())
        self.assertTrue(report['timed_out'])
        self.assert_descendant_dead()

    def test_sigterm_to_the_wrapper_kills_the_suite_group_and_is_reported_as_interrupted(self):
        wrapper = self.start(60)
        self.wait_for_beat()
        wrapper.send_signal(signal.SIGTERM)
        wrapper.wait(30)
        report = json.loads(Path(self.report).read_text())
        self.assertTrue(report['interrupted'])
        self.assertFalse(report['timed_out'])
        self.assert_descendant_dead()


def descendant_suite(beat):
    """A suite that starts a detached heartbeat process (its own session) and then hangs."""
    return [sys.executable, '-c', 'import subprocess, sys, time\n'
            f'code = "import time\\nwhile True:\\n    open({beat!r}, \'w\').write(str(time.time()))\\n    time.sleep(0.1)\\n"\n'
            'subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
            'time.sleep(120)\n']


@unittest.skipUnless(ISOLATION, 'private PID/mount namespaces and the openat2 boundary are unavailable here')
class NamespaceTerminationTests(unittest.TestCase):
    """No worktree is deleted while a process of its isolated suite is alive. Termination is proven by a /proc
    scan, not inferred from the parent `unshare` exiting; unproven termination refuses the delete."""

    def setUp(self):
        self.parent = sc.temp_dir(self, 'ocmutparent-')
        self.flags = sc.temp_dir(self, 'ocmutflags-')
        self.beat = os.path.join(self.flags, 'beat')
        self.addCleanup(mutation_check._ABORT.clear)

    def run_hung(self, outer=4, **extra):
        with unittest.mock.patch.object(mutation_check, 'OUTER_TIMEOUT', outer):
            return mutation_check.run_one(mutation_check.MUTATIONS[0], suite=descendant_suite(self.beat), parent=self.parent)

    def wait_for_beat(self):
        for _ in range(300):
            if os.path.exists(self.beat):
                return
            time.sleep(0.1)
        self.fail('the suite\'s descendant never started')

    def assert_descendant_dead(self):
        self.assertTrue(os.path.exists(self.beat), 'the descendant never ran')
        os.unlink(self.beat)
        time.sleep(0.8)
        self.assertFalse(os.path.exists(self.beat), 'a process of the mutated suite is still running')

    def assert_nothing_holds(self):
        self.assertEqual(mutation_check.processes_left(self.parent, []), ([], ''))
        self.assertEqual(os.listdir(self.parent), [])

    def test_an_outer_timeout_leaves_no_live_descendant_and_is_deleted_only_afterwards(self):
        result = self.run_hung()
        self.assertEqual(result['result'], 'TIMEOUT')
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assert_descendant_dead()
        self.assert_nothing_holds()

    def test_termination_is_proven_by_the_scan_even_when_unshare_cannot_kill_the_namespace_init(self):
        without = [flag for flag in mutation_check.UNSHARE if flag != '--kill-child']
        self.assertNotEqual(without, mutation_check.UNSHARE)
        with unittest.mock.patch.object(mutation_check, 'UNSHARE', without):
            result = self.run_hung()
        self.assertEqual(result['result'], 'TIMEOUT')
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assert_descendant_dead()
        self.assert_nothing_holds()

    def test_the_unshare_command_line_carries_kill_child(self):
        self.assertIn('--kill-child', mutation_check.UNSHARE)
        command = mutation_check.isolated_command(Path('/w'), Path('/r'), ['true'], {'K': 'V'})
        self.assertIn('--kill-child', command)

    def test_unverifiable_termination_refuses_deletion_keeps_the_evidence_and_the_original_timeout(self):
        stuck = lambda work, markers: ([(4242, 'stand-in that never dies')], '')      # noqa: E731
        with unittest.mock.patch.object(mutation_check, 'processes_left', stuck), \
                unittest.mock.patch.object(mutation_check, 'kill_pids', lambda pids: None), \
                unittest.mock.patch.object(mutation_check, 'TERMINATION_ATTEMPTS', 2), \
                unittest.mock.patch.object(mutation_check, 'TERMINATION_PAUSE', 0), \
                unittest.mock.patch.object(mutation_check, 'delete_tree') as deleted:
            result = self.run_hung(outer=3)
        self.addCleanup(self.remove_left_behind)
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'TIMEOUT')
        self.assertFalse(result['termination_verified'])
        self.assertIn('outer timeout', result['detail'])                    # the original failure is kept
        self.assertIn('NOT deleted', result['detail'])
        self.assertIn('4242', result['detail'])
        deleted.assert_not_called()
        self.assertTrue(any(name.startswith('mut-report-') for name in os.listdir(self.parent)))       # evidence kept
        self.assertTrue(any(name.startswith('mut-') and not name.startswith('mut-report-') for name in os.listdir(self.parent)))
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def remove_left_behind(self):
        for name in os.listdir(self.parent):
            path = os.path.join(self.parent, name)
            mutation_check.delete_tree(path, sc.identity(path))

    def test_a_scan_that_cannot_run_is_not_evidence_and_refuses_deletion(self):
        broken = lambda work, markers: ([], 'process scan failed (exit 2): boom')       # noqa: E731
        with unittest.mock.patch.object(mutation_check, 'processes_left', broken), \
                unittest.mock.patch.object(mutation_check, 'delete_tree') as deleted:
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                            parent=self.parent)
        self.addCleanup(self.remove_left_behind)
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'CAUGHT')
        self.assertIn('process scan failed', result['detail'])
        deleted.assert_not_called()

    def test_a_failed_namespace_startup_is_an_error_with_everything_cleaned_and_verified(self):
        with unittest.mock.patch.object(mutation_check, 'UNSHARE', ['unshare', '--no-such-option']):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=self.parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assert_nothing_holds()

    def test_an_exception_while_the_suite_runs_still_terminates_it_before_cleanup(self):
        original = subprocess.Popen.communicate

        def boom(process, *a, **k):
            if 'unshare' in process.args:
                raise RuntimeError('reader failed')
            return original(process, *a, **k)
        with unittest.mock.patch.object(subprocess.Popen, 'communicate', boom):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=descendant_suite(self.beat), parent=self.parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertIn('reader failed', result['detail'])
        self.assertTrue(result['termination_verified'])
        time.sleep(0.5)
        if os.path.exists(self.beat):
            self.assert_descendant_dead()
        self.assert_nothing_holds()

    def test_a_signal_to_the_runner_terminates_every_run_in_flight_and_verifies_before_cleanup(self):
        import threading
        holder = {}
        worker = threading.Thread(target=lambda: holder.update(result=mutation_check.run_one(
            mutation_check.MUTATIONS[0], suite=descendant_suite(self.beat), parent=self.parent)))
        worker.start()
        self.wait_for_beat()
        mutation_check.abort_all(signal.SIGTERM, None)
        worker.join(120)
        self.assertFalse(worker.is_alive())
        result = holder['result']
        self.assertEqual(result['result'], 'ERROR')
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assert_descendant_dead()
        self.assert_nothing_holds()
        later = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=self.parent)
        self.assertEqual((later['result'], later['cleanup']), ('ERROR', 'not started'))     # nothing new starts after a signal

    @unittest.skipIf(PRIV is None, 'needs root or passwordless sudo to mount')
    def test_a_descendant_inside_a_suite_mount_dies_with_the_namespace_before_the_worktree_is_deleted(self):
        suite = ('import os, subprocess, sys, tempfile, time\n'
                 'd = tempfile.mkdtemp(); t = os.path.join(d, "src"); os.mkdir(t)\n'
                 f'subprocess.run({list(PRIV)!r} + ["mount", "--bind", t, t], check=True)\n'
                 f'code = "import os, time\\nos.chdir({{}})\\nwhile True:\\n    open({self.beat!r}, \'w\').write(str(time.time()))\\n    time.sleep(0.1)\\n".format(repr(t))\n'
                 'subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                 'time.sleep(120)\n')
        with unittest.mock.patch.object(mutation_check, 'OUTER_TIMEOUT', 6):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', suite], parent=self.parent)
        self.assertEqual(result['result'], 'TIMEOUT')
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assertEqual([m for m in sc.mount_points() if m.startswith(sc.canonical(self.parent) + '/')], [])
        self.assert_descendant_dead()
        self.assert_nothing_holds()

    def test_processes_the_scan_finds_are_killed_and_deletion_waits_for_an_empty_scan(self):
        rounds = iter([([(4242, 'stand-in')], ''), ([], '')])
        killed = []
        with unittest.mock.patch.object(mutation_check, 'processes_left', lambda work, markers: next(rounds)), \
                unittest.mock.patch.object(mutation_check, 'kill_pids', killed.append), \
                unittest.mock.patch.object(mutation_check, 'TERMINATION_PAUSE', 0):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                                            parent=self.parent)
        self.assertEqual(killed, [[4242]])
        self.assertTrue(result['termination_verified'])
        self.assertEqual((result['result'], result['cleanup']), ('CAUGHT', 'clean'))
        self.assertEqual(os.listdir(self.parent), [])

    def test_a_suite_interrupted_by_a_signal_is_an_error_not_a_verdict(self):
        def fake(work, report, suite, env):
            code = ('import json, sys\n'
                    f'json.dump({{"rc": -9, "timed_out": False, "interrupted": True, "leaked_mounts": 0}}, open({str(report)!r}, "w"))\n'
                    'sys.exit(137)\n')
            return [sys.executable, '-c', code]
        with unittest.mock.patch.object(mutation_check, 'isolated_command', fake):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=self.parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertIn('interrupted by a signal', result['detail'])
        self.assertEqual(os.listdir(self.parent), [])
