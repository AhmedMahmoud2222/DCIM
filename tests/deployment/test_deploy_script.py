import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / 'scripts/deploy-docker-compose.sh'
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
     *.State.ExitCode*) echo 0 ;;
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
                        MOCK_UNHEALTHY_SHA='none', HEALTH_CHECK_TIMEOUT='2', LOCK_TIMEOUT='1')
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

    def test_unhealthy_rollback_fails_closed(self):
        env = dict(self.env, MOCK_FAIL_SHA=self.new, MOCK_UNHEALTHY_SHA=self.old)
        result = self.run_script(env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('ROLLBACK FAILED', result.stderr)
        self.assertEqual((self.repo/'.deployment-sha').read_text().strip(),self.old)

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
