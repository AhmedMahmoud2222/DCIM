import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / 'scripts/deploy-docker-compose.sh'
POLICY = Path(__file__).resolve().parents[2] / 'backend/app/core/secret_policy.py'
KEY = 'A'*43+'='


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'user.name', 'Test')
        (self.repo/'scripts').mkdir()
        (self.repo/'.github/scripts').mkdir(parents=True)
        shutil.copyfile(SOURCE, self.repo/'scripts/deploy-docker-compose.sh')
        (self.repo/'backend/app/core').mkdir(parents=True)
        shutil.copyfile(POLICY, self.repo/'backend/app/core/secret_policy.py')
        (self.repo/'.github/scripts/verify_release_sha.py').write_text('import sys\nsys.exit(0)\n')
        (self.repo/'docker-compose.yml').write_text('services: {}\n')
        (self.repo/'docker-compose.production.yml').write_text('services: {}\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'old')
        self.old = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo/'revision').write_text('new')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'new')
        self.new = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'branch', '-M', 'main')
        bare = self.root/'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True)
        git(self.repo, 'remote', 'add', 'origin', str(bare))
        git(self.repo, 'push', '-q', 'origin', 'main')
        self.bin = self.root/'bin'
        self.bin.mkdir()
        docker = self.bin/'docker'
        docker.write_text('''#!/bin/bash
 echo "$*" >> "$MOCK_CALLS"
 if [[ "$1" == info ]]; then sleep "${MOCK_SLEEP:-0}"; exit 0; fi
 if [[ "$1" == inspect ]]; then
   case "$3" in
     *.State.Status*) if [[ "$4" == migrate || "$4" == bootstrap-privileges ]]; then echo exited; else echo running; fi ;;
     *.State.ExitCode*) if [[ "$4" == migrate && "$(git rev-parse HEAD)" == "$MOCK_MIGRATION_SHA" ]]; then echo 1; else echo 0; fi ;;
     *.State.Health.Status*) echo healthy ;;
   esac
   exit 0
 fi
 if [[ "$*" == *' ps -a -q '* ]]; then echo "${@: -1}"; exit 0; fi
 if [[ "$*" == *' up -d --build'* ]]; then
   sha=$(git rev-parse HEAD)
   if [[ "$sha" == "$MOCK_FAIL_SHA" ]]; then exit 43; fi
 fi
 exit 0
''')
        curl = self.bin/'curl'
        curl.write_text('#!/bin/bash\n[[ "$(git rev-parse HEAD)" != "$MOCK_UNHEALTHY_SHA" ]]\n')
        for tool in (docker, curl):
            tool.chmod(0o755)
        self.calls = self.root/'calls'
        self.env = dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'],
                        DEPLOY_DIR=str(self.repo), BACKUP_DIR=str(self.root/'backup'),
                        LOG_DIR=str(self.root/'log'), GITHUB_TOKEN='mock-test-token',
                        POSTGRES_PASSWORD='disposable', DCIM_APP_PASSWORD='disposable',
                        JWT_SECRET_KEY='disposable', CREDENTIAL_ENCRYPTION_KEY=KEY,
                        MOCK_CALLS=str(self.calls), MOCK_FAIL_SHA='none',
                        MOCK_UNHEALTHY_SHA='none', MOCK_MIGRATION_SHA='none', HEALTH_CHECK_TIMEOUT='2', LOCK_TIMEOUT='1')
        (self.repo/'.env').write_text('PREVIOUS_CONFIG=preserved\n')
        (self.repo/'.deployment-sha').write_text(self.old+'\n')

    def run_script(self, env=None):
        return subprocess.run(['bash',str(self.repo/'scripts/deploy-docker-compose.sh'),self.new,'staging'],
                              cwd=self.repo,env=env or self.env,capture_output=True,text=True,timeout=20)

    def test_success_preserves_distinct_previous(self):
        result = self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(),self.new)
        self.assertEqual((self.repo/'.previous-deployment-sha').read_text().strip(),self.old)
        self.assertEqual((self.repo/'.env').read_text(),'PREVIOUS_CONFIG=preserved\n')

    def test_failure_restores_prior_version_and_configuration(self):
        env = dict(self.env, MOCK_FAIL_SHA=self.new)
        result = self.run_script(env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Rollback recovered',result.stderr)
        self.assertEqual(git(self.repo,'rev-parse','HEAD'),self.old)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(),self.old)
        self.assertEqual((self.repo/'.env').read_text(),'PREVIOUS_CONFIG=preserved\n')
        self.assertIn('down --remove-orphans',self.calls.read_text())
        self.assertNotIn('down -v',self.calls.read_text())

    def test_migration_failure_reaches_rollback(self):
        env = dict(self.env, MOCK_MIGRATION_SHA=self.new)
        result = self.run_script(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Rollback recovered', result.stderr)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(), self.old)

    def test_unhealthy_rollback_fails_closed(self):
        env = dict(self.env, MOCK_FAIL_SHA=self.new, MOCK_UNHEALTHY_SHA=self.old)
        result = self.run_script(env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('ROLLBACK FAILED', result.stderr)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(),self.old)

    # DB-08 (#359): placeholder/example secrets are refused before ANY deployment-side effect.
    def assert_rejected_before_mutation(self, env, expected_name, *leaked):
        head = git(self.repo, 'rev-parse', 'HEAD')
        result = self.run_script(env)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(expected_name, output)
        self.assertIn('insecure or placeholder production secrets', result.stderr)
        for value in leaked:
            self.assertNotIn(value, output)
        self.assertFalse(self.calls.exists(), 'Docker or Compose was invoked before secrets were validated')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(), self.old)
        self.assertFalse((self.repo/'.previous-deployment-sha').exists())
        self.assertEqual((self.repo/'.env').read_text(), 'PREVIOUS_CONFIG=preserved\n')

    def test_jwt_example_rejected_before_any_mutation(self):
        example = 'CHANGE_ME_AT_LEAST_32_CHARACTERS'
        self.assert_rejected_before_mutation(dict(self.env, JWT_SECRET_KEY=example), 'JWT_SECRET_KEY', example)

    def test_database_example_passwords_rejected_before_any_mutation(self):
        self.assert_rejected_before_mutation(dict(self.env, DCIM_APP_PASSWORD='CHANGE_ME_APP'), 'DCIM_APP_PASSWORD', 'CHANGE_ME_APP')
        self.assert_rejected_before_mutation(dict(self.env, POSTGRES_PASSWORD='CHANGE_ME_SUPERUSER'), 'POSTGRES_PASSWORD', 'CHANGE_ME_SUPERUSER')

    def test_invalid_fernet_key_rejected_before_any_mutation(self):
        bad = 'x' * 44
        self.assert_rejected_before_mutation(dict(self.env, CREDENTIAL_ENCRYPTION_KEY=bad), 'CREDENTIAL_ENCRYPTION_KEY', bad)

    def test_dotenv_placeholder_is_rejected_when_the_environment_does_not_override_it(self):
        env = {k: v for k, v in self.env.items() if k != 'JWT_SECRET_KEY'}
        (self.repo/'.env').write_text('JWT_SECRET_KEY=CHANGE_ME_AT_LEAST_32_CHARACTERS\n')
        result = self.run_script(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('JWT_SECRET_KEY', result.stderr)
        self.assertNotIn('CHANGE_ME_AT_LEAST_32_CHARACTERS', result.stdout + result.stderr)
        self.assertFalse(self.calls.exists())

    def test_environment_value_overrides_dotenv_placeholder(self):
        (self.repo/'.env').write_text('JWT_SECRET_KEY=CHANGE_ME_AT_LEAST_32_CHARACTERS\n')
        result = self.run_script(dict(self.env, JWT_SECRET_KEY='generated-' + 'k' * 40))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unusual_generated_passwords_are_accepted(self):
        env = dict(self.env, DCIM_APP_PASSWORD="p@ss/w:rd#1?&=+%$'\"x9Zk", POSTGRES_PASSWORD='Zq8-Lm_2.v~Tn' + 'a' * 20)
        result = self.run_script(env)
        self.assertEqual(result.returncode, 0, result.stderr)

    # Provenance (AGY P3, PR #362): the requested release's own policy judges the secrets.
    def commit_release(self, message, policy_text):
        policy = self.repo/'backend/app/core/secret_policy.py'
        if policy_text is None:
            git(self.repo, 'rm', '-q', 'backend/app/core/secret_policy.py')
        else:
            policy.write_text(policy_text)
            git(self.repo, 'add', 'backend/app/core/secret_policy.py')
        git(self.repo, 'commit', '-qm', message)
        sha = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'push', '-q', 'origin', 'main')
        git(self.repo, 'checkout', '-q', '--detach', self.new)  # the deploy host is still on the previous release
        return sha

    def test_release_policy_not_the_checked_out_policy_is_applied(self):
        stricter = 'import sys\nprint("release-policy-applied", file=sys.stderr)\nsys.exit(1)\n'
        release = self.commit_release('release with stricter policy', stricter)
        result = subprocess.run(['bash', str(self.repo/'scripts/deploy-docker-compose.sh'), release, 'staging'],
                                cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('release-policy-applied', result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.new)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(), self.old)

    def test_release_that_predates_the_policy_is_judged_by_the_current_one(self):
        release = self.commit_release('release without policy file', None)
        env = dict(self.env, JWT_SECRET_KEY='CHANGE_ME_AT_LEAST_32_CHARACTERS')
        result = subprocess.run(['bash', str(self.repo/'scripts/deploy-docker-compose.sh'), release, 'staging'],
                                cwd=self.repo, env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('JWT_SECRET_KEY', result.stderr)
        self.assertNotIn('CHANGE_ME_AT_LEAST_32_CHARACTERS', result.stdout + result.stderr)
        self.assertFalse(self.calls.exists())

    def test_two_process_lock_and_stable_inode(self):
        env = dict(self.env, MOCK_SLEEP='3')
        first = subprocess.Popen(['bash',str(self.repo/'scripts/deploy-docker-compose.sh'),self.new,'staging'],
                                 cwd=self.repo, env=env, stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            lock=self.repo/'.deployment.lock'
            for _ in range(100):
                if lock.exists() and first.poll() is None and self.calls.exists() and 'info' in self.calls.read_text():
                    break
                time.sleep(.03)
            inode=lock.stat().st_ino
            second=self.run_script(env)
            self.assertNotEqual(second.returncode,0)
            self.assertIn('lock unavailable',second.stderr)
            self.assertEqual(inode,lock.stat().st_ino)
            out, err=first.communicate(timeout=12)
            self.assertEqual(first.returncode,0,err)
            self.assertEqual(inode,lock.stat().st_ino)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()


if __name__ == '__main__':
    unittest.main()
