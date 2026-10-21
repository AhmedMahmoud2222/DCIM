"""Tests for the test-account lifecycle and the mutation runner's identity/guard behaviour (tests v1.0).

Account tests use a fake account database unless a real, uniquely named throwaway account is explicitly
exercised; the real `ocprobe` account is never touched here.
"""
import importlib
import multiprocessing
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_lease as al  # noqa: E402
import strict_cleanup as sc  # noqa: E402

mutation_check = importlib.import_module('mutation_check')
runtime = importlib.import_module('test_opencode_runtime')

PRIV = runtime.PRIV
MOUNTS = runtime.MOUNTS_AVAILABLE


class FakePasswd:
    """An in-memory account database that records every change."""

    def __init__(self, accounts=None, running=(), fail_remove=False, zombie=False, remove_raises=None):
        self.accounts = dict(accounts or {})
        self.running_names = set(running)
        self.fail_remove = fail_remove
        self.zombie = zombie                # userdel reports success but the account stays
        self.remove_raises = remove_raises
        self.log = []

    def comment(self, name):
        return self.accounts.get(name)

    def create(self, name):
        self.log.append(('create', name))
        self.accounts[name] = f'{al.MARKER} (created by the deployment tests)'

    def remove(self, name):
        self.log.append(('remove', name))
        if self.remove_raises:
            raise self.remove_raises
        if self.fail_remove:
            return False
        if not self.zombie:
            self.accounts.pop(name, None)
        return True

    def running(self, name):
        return name in self.running_names


class AccountLeaseTests(unittest.TestCase):
    def setUp(self):
        self.leases = sc.temp_dir(self, 'ocacct-leases-')
        self.passwd = FakePasswd()

    def acquire(self, name='ocprobe', passwd=None):
        return al.acquire(name, [], passwd=passwd or self.passwd, lease_root=self.leases)

    def test_a_missing_account_is_created_with_the_marker_and_removed_on_release(self):
        lease = self.acquire()
        self.assertTrue(lease.created)
        self.assertEqual(self.passwd.log, [('create', 'ocprobe')])
        self.assertTrue(self.passwd.accounts['ocprobe'].startswith(al.MARKER))
        lease.release()
        self.assertNotIn('ocprobe', self.passwd.accounts)

    def test_a_pre_existing_account_is_never_modified_or_removed(self):
        self.passwd.accounts['ocprobe'] = 'someone else created this'
        lease = self.acquire()
        self.assertEqual(lease.role, 'preexisting')
        lease.release()
        self.assertEqual(self.passwd.accounts, {'ocprobe': 'someone else created this'})
        self.assertEqual(self.passwd.log, [])

    def test_a_pre_existing_account_whose_name_merely_resembles_the_marker_is_still_removed_only_if_it_carries_it(self):
        self.passwd.accounts['ocprobe'] = 'not-' + al.MARKER
        lease = self.acquire()
        lease.release()
        self.assertIn('ocprobe', self.passwd.accounts)

    def test_two_workers_share_one_account_and_the_last_to_finish_removes_it(self):
        first = self.acquire()
        second = self.acquire()
        self.assertEqual((first.role, second.role), ('created', 'joined'))
        self.assertEqual([c for c in self.passwd.log if c[0] == 'create'], [('create', 'ocprobe')])
        first.release()                                            # the creator finishes first
        self.assertIn('ocprobe', self.passwd.accounts)             # the other worker still needs it
        second.release()
        self.assertNotIn('ocprobe', self.passwd.accounts)

    def test_release_is_idempotent(self):
        lease = self.acquire()
        lease.release()
        lease.release()
        self.assertEqual([c for c in self.passwd.log if c[0] == 'remove'], [('remove', 'ocprobe')])

    def test_an_account_with_running_processes_is_not_removed_and_that_is_reported(self):
        lease = self.acquire()
        self.passwd.running_names.add('ocprobe')
        with self.assertRaises(al.AccountInUse):
            lease.release()
        self.assertIn('ocprobe', self.passwd.accounts)

    def test_a_failed_removal_is_reported(self):
        self.passwd.fail_remove = True
        lease = self.acquire()
        with self.assertRaises(al.AccountCleanupFailed):
            lease.release()

    def test_leases_of_dead_processes_do_not_keep_the_account_alive(self):
        stale = os.path.join(self.leases, 'ocprobe.999999.deadbeef')
        Path(stale).write_text('999999 12345 created\n')
        lease = self.acquire()
        lease.release()
        self.assertNotIn('ocprobe', self.passwd.accounts)
        self.assertFalse(os.path.exists(stale))

    def test_a_failure_after_creating_the_account_removes_it_again(self):
        with unittest.mock.patch('builtins.open', side_effect=OSError('disk full')) as patched:
            real_open = open

            def selective(path, *a, **k):
                if str(path).startswith(self.leases) and not str(path).endswith('.lock'):
                    raise OSError('disk full')
                return real_open(path, *a, **k)

            patched.side_effect = selective
            with self.assertRaises(OSError):
                self.acquire()
        self.assertNotIn('ocprobe', self.passwd.accounts)
        self.assertEqual(self.passwd.log, [('create', 'ocprobe'), ('remove', 'ocprobe')])

    def failing_lease_file(self):
        real_open = open

        def selective(path, *a, **k):
            if str(path).startswith(self.leases) and not str(path).endswith('.lock'):
                raise OSError('disk full while writing the lease')
            return real_open(path, *a, **k)

        return unittest.mock.patch('builtins.open', selective)

    def test_a_lease_file_failure_rolls_the_new_account_back_and_keeps_the_original_error(self):
        with self.failing_lease_file(), self.assertRaises(OSError) as caught:
            self.acquire()
        self.assertIn('disk full', str(caught.exception))
        self.assertNotIn('ocprobe', self.passwd.accounts)
        self.assertEqual(self.passwd.log, [('create', 'ocprobe'), ('remove', 'ocprobe')])

    def test_a_failed_userdel_after_a_lease_failure_is_reported_with_the_original_error(self):
        self.passwd.fail_remove = True
        with self.failing_lease_file(), self.assertRaises(al.AccountCleanupFailed) as caught:
            self.acquire()
        message = str(caught.exception)
        self.assertIn('disk full while writing the lease', message)          # the original error is in the report
        self.assertIn('could not be rolled back', message)
        self.assertIn('remove it by hand', message)
        self.assertIsInstance(caught.exception.__cause__, OSError)            # and preserved as the cause
        self.assertIn('ocprobe', self.passwd.accounts)                        # the leftover is real, not hidden

    def test_a_userdel_that_claims_success_but_leaves_the_account_is_still_a_rollback_failure(self):
        self.passwd.zombie = True
        with self.failing_lease_file(), self.assertRaises(al.AccountCleanupFailed) as caught:
            self.acquire()
        self.assertIn('still present', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_an_exception_from_the_rollback_itself_is_reported_with_the_original_error(self):
        self.passwd.remove_raises = PermissionError('userdel not permitted')
        with self.failing_lease_file(), self.assertRaises(al.AccountCleanupFailed) as caught:
            self.acquire()
        message = str(caught.exception)
        self.assertIn('userdel not permitted', message)
        self.assertIn('disk full while writing the lease', message)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_a_lease_failure_never_removes_a_pre_existing_account(self):
        self.passwd.accounts['ocprobe'] = 'created by somebody else'
        with self.failing_lease_file(), self.assertRaises(OSError):
            self.acquire()
        self.assertEqual(self.passwd.accounts, {'ocprobe': 'created by somebody else'})
        self.assertEqual(self.passwd.log, [])

    def test_a_lease_failure_never_removes_an_account_this_call_only_joined(self):
        self.passwd.accounts['ocprobe'] = f'{al.MARKER} (created by the deployment tests)'
        with self.failing_lease_file(), self.assertRaises(OSError):
            self.acquire()
        self.assertIn('ocprobe', self.passwd.accounts)
        self.assertEqual(self.passwd.log, [])

    def test_setup_and_rollback_errors_both_reach_the_test_result_in_a_class_setup(self):
        self.passwd.fail_remove = True

        def set_up(cls):
            with self.failing_lease_file():
                al.lease_for_class(cls, 'ocprobe', [], passwd=self.passwd, lease_root=self.leases)

        result = self.run_inner(set_up)
        self.assertEqual(len(result.errors), 1)
        text = result.errors[0][1]
        self.assertIn('AccountCleanupFailed', text)
        self.assertIn('disk full while writing the lease', text)
        self.assertIn('OSError', text)                                         # the chained original is in the traceback

    def run_inner(self, set_up):
        class Inner(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                set_up(cls)

            def test_it(self):
                pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(result)
        return result

    def test_the_account_is_removed_after_a_class_that_succeeds(self):
        result = self.run_inner(lambda cls: al.lease_for_class(cls, 'ocprobe', [], passwd=self.passwd, lease_root=self.leases))
        self.assertTrue(result.wasSuccessful())
        self.assertNotIn('ocprobe', self.passwd.accounts)

    def test_the_account_is_removed_when_a_later_setup_step_raises(self):
        def set_up(cls):
            al.lease_for_class(cls, 'ocprobe', [], passwd=self.passwd, lease_root=self.leases)
            raise OSError('copy failed after the account was created')

        result = self.run_inner(set_up)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('copy failed', result.errors[0][1])
        self.assertNotIn('ocprobe', self.passwd.accounts)

    def test_a_cleanup_failure_is_visible_beside_the_setup_error(self):
        self.passwd.fail_remove = True

        def set_up(cls):
            al.lease_for_class(cls, 'ocprobe', [], passwd=self.passwd, lease_root=self.leases)
            raise OSError('copy failed')

        result = self.run_inner(set_up)
        text = ' '.join(e[1] for e in result.errors)
        self.assertEqual(len(result.errors), 2)
        self.assertIn('copy failed', text)
        self.assertIn('could not remove the suite-owned account', text)

    def test_the_runtime_test_class_leases_the_account_before_any_other_setup(self):
        calls = []
        real_lease = al.lease_for_class

        def spy(cls, name, priv, **kwargs):
            calls.append(('lease', name))
            return real_lease(cls, name, priv, passwd=self.passwd, lease_root=self.leases)

        private = sc.temp_dir(self, 'ocprivtmp-')
        with unittest.mock.patch.object(runtime, 'lease_for_class', spy), \
                unittest.mock.patch.object(runtime.shutil, 'copy', side_effect=OSError('copy failed')), \
                unittest.mock.patch.object(tempfile, 'tempdir', private):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(runtime.RuntimeFilesystemTests).run(result)
        self.assertEqual(calls, [('lease', runtime.AGENT)])
        self.assertTrue(any('copy failed' in e[1] for e in result.errors))
        self.assertNotIn(runtime.AGENT, self.passwd.accounts)      # created by the lease, removed by the class cleanup
        self.assertEqual(os.listdir(private), [])


def _hold(lease_root, name, priv, ready, release, results):
    passwd = al.Passwd(priv)
    lease = al.acquire(name, priv, passwd=passwd, lease_root=lease_root)
    results.put((os.getpid(), lease.role))
    ready.set()
    release.wait(30)
    lease.release()


@unittest.skipIf(PRIV is None, 'needs root or passwordless sudo to create a real account')
class RealAccountTests(unittest.TestCase):
    """A real, uniquely named throwaway account, never `ocprobe`."""

    def setUp(self):
        self.name = 'octmp' + secrets.token_hex(3)
        self.leases = sc.temp_dir(self, 'ocacct-leases-')
        self.passwd = al.Passwd(PRIV)
        self.addCleanup(lambda: self.passwd.comment(self.name) is not None and self.passwd.remove(self.name))

    def acquire(self):
        return al.acquire(self.name, PRIV, passwd=self.passwd, lease_root=self.leases)

    def test_a_real_account_is_created_marked_and_removed(self):
        lease = self.acquire()
        self.assertTrue(self.passwd.comment(self.name).startswith(al.MARKER))
        lease.release()
        self.assertIsNone(self.passwd.comment(self.name))

    def test_a_real_pre_existing_account_survives_a_full_lease_cycle(self):
        subprocess.run([*PRIV, 'useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', self.name], check=True)
        before = subprocess.run(['getent', 'passwd', self.name], capture_output=True, text=True).stdout
        lease = self.acquire()
        self.assertEqual(lease.role, 'preexisting')
        lease.release()
        self.assertEqual(subprocess.run(['getent', 'passwd', self.name], capture_output=True, text=True).stdout, before)

    def test_parallel_processes_share_the_account_and_only_the_last_removes_it(self):
        ctx = multiprocessing.get_context('fork')
        results = ctx.Queue()
        events = [(ctx.Event(), ctx.Event()) for _ in range(3)]
        workers = [ctx.Process(target=_hold, args=(self.leases, self.name, PRIV, ready, release, results)) for ready, release in events]
        for worker in workers:
            worker.start()
        roles = []
        for ready, _release in events:
            self.assertTrue(ready.wait(30))
        for _ in workers:
            roles.append(results.get(timeout=10)[1])
        self.assertEqual(sorted(roles), ['created', 'joined', 'joined'])
        for index, (_ready, release) in enumerate(events):
            release.set()
            workers[index].join(30)
            self.assertEqual(workers[index].exitcode, 0)
            if index < 2:
                self.assertIsNotNone(self.passwd.comment(self.name), 'removed while another worker still used it')
        self.assertIsNone(self.passwd.comment(self.name))

    def test_a_real_account_with_a_running_process_is_not_deleted(self):
        lease = self.acquire()
        sleeper = subprocess.Popen([*PRIV, 'setpriv', f'--reuid={self.name}', f'--regid={self.name}', '--clear-groups', 'sleep', '30'])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        for _ in range(50):
            if self.passwd.running(self.name):
                break
            subprocess.run(['true'])
        if not self.passwd.running(self.name):
            self.skipTest('could not start a process as the throwaway account')
        with self.assertRaises(al.AccountInUse):
            lease.release()
        self.assertIsNotNone(self.passwd.comment(self.name))
        sleeper.kill()
        sleeper.wait()
        subprocess.run([*PRIV, 'pkill', '-9', '-u', self.name], check=False)
        for _ in range(100):                                       # let the killed process disappear
            if not self.passwd.running(self.name):
                break
            time.sleep(0.05)
        al.Lease(self.name, 'created', os.path.join(self.leases, 'x'), self.passwd, lease.lock_path).release()
        self.assertIsNone(self.passwd.comment(self.name))


class MutationWorktreeGuardTests(unittest.TestCase):
    """cleanup_worktree must verify identity before it looks at or detaches any mount; run_one must guard
    the worktree from the moment mkdtemp succeeds."""

    def worktree(self):
        work = Path(tempfile.mkdtemp(prefix='mut-test-'))
        self.addCleanup(lambda: work.exists() and shutil.rmtree(work, True))
        (work / 'sub').mkdir()
        return work, sc.identity(str(work))

    def table_for(self, work):
        return f'1 2 0:3 / {work}/sub rw - ext4 /dev/vda rw\n'

    def test_a_replaced_worktree_is_refused_and_nothing_in_the_replacement_is_touched(self):
        work, ident = self.worktree()
        os.rename(work, str(work) + '.moved')
        self.addCleanup(shutil.rmtree, str(work) + '.moved', True)
        work.mkdir()
        (work / 'sub').mkdir()
        (work / 'sub' / 'unrelated.txt').write_text('unrelated')
        deleted = []
        with unittest.mock.patch.object(mutation_check, 'delete_tree', lambda p, e: deleted.append(p)):
            outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('identity changed', outcome['detail'])
        self.assertEqual(deleted, [])                              # not even a delete was attempted
        self.assertEqual((work / 'sub' / 'unrelated.txt').read_text(), 'unrelated')

    def test_a_renamed_worktree_and_a_symlink_in_its_place_are_refused(self):
        work, ident = self.worktree()
        os.rename(work, str(work) + '.moved')
        self.addCleanup(shutil.rmtree, str(work) + '.moved', True)
        os.symlink(str(work) + '.moved', work)
        self.addCleanup(lambda: os.path.islink(work) and os.unlink(work))
        deleted = []
        with unittest.mock.patch.object(mutation_check, 'delete_tree', lambda p, e: deleted.append(p)):
            outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertEqual(deleted, [])

    def test_a_mount_visible_in_the_host_namespace_is_a_failure_and_nothing_is_unmounted_or_deleted(self):
        work, ident = self.worktree()
        table = self.table_for(work)
        deleted, unmounted = [], []
        with unittest.mock.patch.object(sc, 'read_mountinfo', lambda: table), \
                unittest.mock.patch.object(mutation_check, 'delete_tree', lambda p, e: deleted.append(p)), \
                unittest.mock.patch.object(subprocess, 'run', lambda *a, **k: unmounted.append(a)):
            outcome = mutation_check.cleanup_worktree(work, ident, leaked=1)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertIn('host namespace', outcome['detail'])
        self.assertIn('nothing was unmounted or deleted', outcome['detail'])
        self.assertEqual((deleted, unmounted), ([], []))

    @unittest.skipUnless(MOUNTS, 'bind mounts are not permitted in this environment')
    def test_an_unrelated_live_mount_in_a_replacement_directory_stays_mounted(self):
        work, ident = self.worktree()
        os.rename(work, str(work) + '.moved')
        self.addCleanup(shutil.rmtree, str(work) + '.moved', True)
        work.mkdir()
        (work / 'sub').mkdir()
        (work / 'sub' / 'unrelated.txt').write_text('unrelated')
        target = str(work / 'sub')
        subprocess.run([*PRIV, 'mount', '--bind', target, target], check=True)
        self.addCleanup(runtime.unmount_all, target, 0)
        outcome = mutation_check.cleanup_worktree(work, ident)
        self.assertEqual(outcome['state'], 'FAILED')
        self.assertEqual(len(sc.mounts_at(target)), 1)             # still mounted: nothing was detached
        self.assertEqual((work / 'sub' / 'unrelated.txt').read_text(), 'unrelated')

    def test_a_failure_while_recording_identity_is_guarded_and_the_empty_worktree_is_removed(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        with unittest.mock.patch.object(sc, 'identity', side_effect=OSError('inodes exhausted')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertIn('inodes exhausted', result['detail'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assertEqual(os.listdir(parent), [])

    def test_an_unrecorded_worktree_that_cannot_be_removed_is_cleanup_failed(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d)) for d in os.listdir(parent)])
        with unittest.mock.patch.object(sc, 'identity', side_effect=OSError('inodes exhausted')), \
                unittest.mock.patch.object(mutation_check.os, 'rmdir', side_effect=OSError('busy')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'ERROR')
        self.assertIn('could not be removed', result['detail'])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def test_a_failing_chmod_after_identity_capture_is_cleaned_up_and_reported(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        with unittest.mock.patch.object(mutation_check.os, 'chmod', side_effect=OSError('read-only filesystem')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertEqual(result['cleanup'], 'clean')
        self.assertEqual(os.listdir(parent), [])
        self.assertEqual(mutation_check.exit_code(mutation_check.summarize([result])), 1)

    def test_a_failing_scratch_directory_creation_is_cleaned_up_and_reported(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        real_mkdir = Path.mkdir

        def refuse_scratch(self_path, *args, **kwargs):
            if self_path.name == 'tmp':
                raise OSError('no space left')
            return real_mkdir(self_path, *args, **kwargs)

        with unittest.mock.patch.object(Path, 'mkdir', refuse_scratch):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
        self.assertEqual(result['result'], 'ERROR')
        self.assertIn('no space left', result['detail'])
        self.assertEqual(result['cleanup'], 'clean')
        self.assertEqual(os.listdir(parent), [])

    def test_a_cleanup_failure_after_a_worker_error_keeps_both_facts(self):
        parent = sc.temp_dir(self, 'ocmutparent-')
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(parent, d)) for d in os.listdir(parent)])
        with unittest.mock.patch.object(mutation_check.os, 'chmod', side_effect=OSError('read-only filesystem')), \
                unittest.mock.patch.object(mutation_check, 'delete_tree', side_effect=OSError('device busy')):
            result = mutation_check.run_one(mutation_check.MUTATIONS[0], suite=[sys.executable, '-c', 'pass'], parent=parent)
        self.assertEqual(result['result'], 'CLEANUP_FAILED')
        self.assertEqual(result['suite_result'], 'ERROR')
        self.assertIn('read-only filesystem', result['detail'])
        self.assertIn('device busy', result['detail'])


if __name__ == '__main__':
    unittest.main()
