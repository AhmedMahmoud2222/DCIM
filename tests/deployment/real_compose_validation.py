#!/usr/bin/env python3
"""Disposable real-Docker validation of the combined Compose deployment and rollback.

GitHub release verification is replaced only in the temporary Git fixture because a
PR SHA is intentionally not eligible for the production release gate. All Compose,
PostgreSQL, migration, bootstrap, HTTP, Celery, flock and rollback operations use
real processes and real Docker Engine containers. Never run against a shared host.
"""
import base64
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ('postgres', 'redis', 'migrate', 'bootstrap-privileges',
            'backend', 'celery-worker', 'celery-beat', 'frontend')
LONG_RUNNING = ('postgres', 'redis', 'backend', 'celery-worker', 'celery-beat', 'frontend')


def run(args, *, cwd=None, env=None, timeout=600, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and result.returncode:
        raise AssertionError(f"command failed (exit {result.returncode}): {args[0]} {args[1] if len(args)>1 else ''}")
    return result


def git(repo, *args):
    return run(['git', '-C', str(repo), *args]).stdout.strip()


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mask(text, env):
    for key in ('POSTGRES_PASSWORD', 'DCIM_APP_PASSWORD', 'JWT_SECRET_KEY', 'CREDENTIAL_ENCRYPTION_KEY'):
        text = text.replace(env[key], '[REDACTED]')
    text = re.sub(r'(?i)(password|secret|token|key)\s*[:=]\s*\S+', r'\1=[REDACTED]', text)
    text = re.sub(r'\w+://[^\s/@]+:[^\s/@]+@', '[REDACTED-URL]@', text)
    return ''.join(c for c in text if c == '\n' or c == '\t' or 32 <= ord(c) < 127)


class Validation:
    def __init__(self):
        self.project = 'dcim-stage-test-' + secrets.token_hex(6)
        self.tmp = Path(tempfile.mkdtemp(prefix=self.project+'-', dir=os.getenv('RUNNER_TEMP','/tmp')))
        self.repo = self.tmp/'repo'
        self.repo.mkdir()
        self.data = self.tmp/'postgres'
        self.origin = self.tmp/'origin.git'
        self.bin = self.tmp/'bin'
        self.bin.mkdir()
        self.good = None
        self.latest = None
        self.env = dict(os.environ)
        self.env.update(COMPOSE_PROJECT_NAME=self.project, DEPLOY_DIR=str(self.repo),
                        BACKUP_DIR=str(self.tmp/'backups'), LOG_DIR=str(self.tmp/'logs'),
                        POSTGRES_DATA_PATH=str(self.data), POSTGRES_DB='dcim_stage',
                        POSTGRES_PASSWORD=secrets.token_urlsafe(32),
                        DCIM_APP_PASSWORD=secrets.token_urlsafe(32),
                        JWT_SECRET_KEY=secrets.token_urlsafe(48),
                        CREDENTIAL_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode(),
                        CORS_ALLOWED_ORIGINS='["http://localhost:8080"]',
                        HEALTH_CHECK_TIMEOUT='120', LOCK_TIMEOUT='2',
                        DCIM_TEST_REPO=str(self.repo), DCIM_TEST_FAIL_ROLLBACK='0',
                        DCIM_TEST_DOCKER_DELAY_INFO='0')
        self.env['PATH'] = str(self.bin)+os.pathsep+self.env['PATH']
        github = os.getenv('GITHUB_ENV')
        if github:
            for key in ('POSTGRES_PASSWORD', 'DCIM_APP_PASSWORD','JWT_SECRET_KEY','CREDENTIAL_ENCRYPTION_KEY'):
                print('::add-mask::'+self.env[key], flush=True)
        # No credentials or configuration are uploaded as artifacts.

    def compose(self, *args, check=True, timeout=600):
        return run(['docker','compose','-f','docker-compose.yml','-f','docker-compose.production.yml',*args],
                   cwd=self.repo,env=self.env,check=check,timeout=timeout)

    def setup(self):
        # Archive the exact PR checkout, avoiding any shared repository or Git ref mutation.
        archive = subprocess.Popen(['git','-C',str(ROOT),'archive','HEAD'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        extracted = subprocess.run(['tar','-xf','-','-C',str(self.repo)],stdin=archive.stdout,capture_output=True)
        archive.stdout.close()
        if archive.wait() or extracted.returncode:
            raise AssertionError('could not extract PR checkout into disposable repository')
        # The test-only verifier is deliberately confined to the disposable copy.
        stub = self.repo/'.github/scripts/verify_release_sha.py'
        stub.write_text('import re,sys\nsys.exit(0 if len(sys.argv)==2 and re.fullmatch(r"[0-9a-f]{40}",sys.argv[1]) else 1)\n')
        wrapper = self.bin/'docker'
        wrapper.write_text('''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == info && "${DCIM_TEST_DOCKER_DELAY_INFO:-0}" != 0 ]]; then
    sleep "$DCIM_TEST_DOCKER_DELAY_INFO"
fi
if [[ "${DCIM_TEST_FAIL_ROLLBACK:-0}" == 1 && " $* " == *" up -d --build "* &&
      "$(git -C "$DCIM_TEST_REPO" rev-parse HEAD)" == "$DCIM_TEST_GOOD_SHA" ]]; then
    echo 'TEST-ONLY injected rollback Docker failure' >&2
    exit 91
fi
exec /usr/bin/docker "$@"
''')
        wrapper.chmod(0o755)
        git(self.repo,'init','-q')
        git(self.repo,'config','user.email','disposable@example.invalid')
        git(self.repo,'config','user.name','Disposable validation')
        git(self.repo,'add','.')
        git(self.repo,'commit','-qm','disposable baseline')
        self.baseline = git(self.repo,'rev-parse','HEAD')
        self.latest = self.baseline
        git(self.repo,'branch','-M','main')
        run(['git','init','-q','--bare',str(self.origin)])
        git(self.repo,'remote','add','origin',str(self.origin))
        git(self.repo,'push','-q','origin','main')
        # .env is untracked and mode 0600. The runner, not the application, owns it.
        env_file = self.repo/'.env'
        env_file.write_text(''.join(f'{key}={self.env[key]}\n' for key in (
            'POSTGRES_PASSWORD','POSTGRES_DB','DCIM_APP_PASSWORD','JWT_SECRET_KEY',
            'CREDENTIAL_ENCRYPTION_KEY','POSTGRES_DATA_PATH','CORS_ALLOWED_ORIGINS')))
        env_file.chmod(0o600)
        self.env_hash = sha_file(env_file)
        self.check_bindings()
        print('PASS: isolated Git fixture, generated masked credentials and effective localhost bindings')

    def check_bindings(self):
        import json
        cfg=json.loads(self.compose('config','--format','json').stdout)
        for service in ('postgres','redis','backend','frontend'):
            ports=cfg['services'][service]['ports']
            assert len(ports)==1 and ports[0]['host_ip']=='127.0.0.1', service
        assert any(m['type']=='bind' and m['target']=='/var/lib/postgresql/data' and
                   m['source']==str(self.data) for m in cfg['services']['postgres']['volumes'])

    def deploy(self, sha, *, fail_rollback=False, delay_info=0):
        env=dict(self.env,DCIM_TEST_GOOD_SHA=self.good or '',
                 DCIM_TEST_FAIL_ROLLBACK='1' if fail_rollback else '0',
                 DCIM_TEST_DOCKER_DELAY_INFO=str(delay_info))
        return run(['bash',str(self.repo/'scripts/deploy-docker-compose.sh'),sha,'staging'],
                   cwd=self.repo,env=env,timeout=780,check=False)

    def states(self):
        details={}
        for service in SERVICES:
            id=self.compose('ps','-a','-q',service,check=False).stdout.strip()
            if not id:
                details[service]=('missing','missing','missing')
                continue
            fields=[]
            for field in ('.State.Status','.State.ExitCode','.State.Health.Status'):
                result=run(['/usr/bin/docker','inspect','--format','{{'+field+'}}',id],check=False)
                fields.append(result.stdout.strip() if result.returncode==0 else 'unavailable')
            details[service]=tuple(fields)
        return details

    def assert_healthy(self):
        states=self.states()
        for service in ('postgres','redis','backend'):
            assert states[service][0]=='running' and states[service][2]=='healthy', states
        for service in ('celery-worker','celery-beat','frontend'):
            assert states[service][0]=='running', states
        for service in ('migrate','bootstrap-privileges'):
            assert states[service][:2]==('exited','0'), states
        for url in ('http://127.0.0.1:8000/api/v1/health/ready',
                    'http://127.0.0.1:8080/api/v1/health/ready','http://127.0.0.1:8080/'):
            with urllib.request.urlopen(url,timeout=5) as response:
                assert response.status==200, url
        print('PASS: '+', '.join(f'{k}={v[0]}/{v[1]}/{v[2]}' for k,v in states.items()))
        print('PASS: backend and frontend-proxied readiness, frontend HTTP 200')

    def marker(self, create=False):
        sql=('CREATE TABLE IF NOT EXISTS disposable_staging_marker (id integer PRIMARY KEY); '
             'INSERT INTO disposable_staging_marker VALUES (1) ON CONFLICT DO NOTHING;' if create else
             'SELECT count(*) FROM disposable_staging_marker WHERE id=1;')
        output=self.compose('exec','-T','postgres','psql','-U','postgres','-d','dcim_stage',
                            '-Atc',sql).stdout
        if not create:
            assert output.strip()=='1', 'persistent PostgreSQL marker not recovered'
            assert run(['sudo','test','-f',str(self.data/'PG_VERSION')],check=False).returncode==0, 'PostgreSQL storage disappeared'

    def next_commit(self, kind):
        # A fresh synthetic main commit; never force push or change the real repository.
        git(self.repo,'checkout','-q','-B','main',self.latest)
        path=self.repo/'docker-compose.production.yml'
        baseline=git(self.repo,'show',self.baseline+':docker-compose.production.yml')+'\n'
        if kind=='migration':
            anchor='    command: ["alembic", "upgrade", "head"]'
            changed='    command: ["sh", "-c", "exit 67"]'
        elif kind=='bootstrap':
            anchor='    entrypoint: ["psql", "-v", "ON_ERROR_STOP=1", "-f", "/bootstrap_privileged_roles.sql"]'
            changed='    entrypoint: ["sh", "-c", "exit 68"]'
        elif kind=='backend':
            anchor='  backend:\n    build:\n      context: ./backend\n      target: runtime\n'
            changed=anchor+'    command: ["sh", "-c", "exit 69"]\n'
        else:
            raise AssertionError(kind)
        assert baseline.count(anchor)==1, 'failure fixture no longer matches production Compose'
        path.write_text(baseline.replace(anchor,changed))
        git(self.repo,'add','docker-compose.production.yml')
        git(self.repo,'commit','--allow-empty','-qm','test-only '+kind+' failure fixture')
        self.latest=git(self.repo,'rev-parse','HEAD')
        git(self.repo,'push','-q','origin','main')
        return self.latest

    def assert_state(self,current,previous=None):
        assert (self.repo/'.deployment-sha').read_text().strip()==current
        assert git(self.repo,'rev-parse','HEAD')==current
        if previous:
            assert (self.repo/'.previous-deployment-sha').read_text().strip()==previous
        assert sha_file(self.repo/'.env')==self.env_hash, 'prior configuration changed'

    def diagnose(self, label):
        print('Sanitized diagnostics for '+label+': '+str(self.states()))
        for service in ('migrate','bootstrap-privileges','backend'):
            id=self.compose('ps','-a','-q',service,check=False).stdout.strip()
            if not id:
                continue
            result=run(['/usr/bin/docker','logs','--tail','20',id],check=False)
            text=mask(result.stdout+'\n'+result.stderr,self.env)
            relevant=[x[:180] for x in text.splitlines() if re.search('error|fail|exit|Traceback|migration|bootstrap',x,re.I)]
            for line in relevant[-3:]:
                print(f'{service} log: {line}')

    def test(self):
        self.setup()
        first=self.deploy(self.baseline)
        assert first.returncode==0, mask(first.stderr[-800:],self.env)
        self.good=self.baseline
        self.assert_healthy()
        self.assert_state(self.baseline)
        self.marker(create=True)
        print('PASS: initial empty-database migrations and privilege bootstrap')

        # The deployment checkout is detached. Move only the disposable local main
        # back to its verified baseline before creating and pushing release B.
        git(self.repo,'checkout','-q','-B','main',self.baseline)
        # New harmless release B, concurrent attempts with real flock and Docker.
        (self.repo/'validation_revision.txt').write_text('safe version B\n')
        git(self.repo,'add','validation_revision.txt')
        git(self.repo,'commit','-qm','disposable good release B')
        self.good=git(self.repo,'rev-parse','HEAD')
        self.latest=self.good
        git(self.repo,'push','-q','origin','main')
        env=dict(self.env,DCIM_TEST_GOOD_SHA=self.good,DCIM_TEST_DOCKER_DELAY_INFO='6')
        first=subprocess.Popen(['bash',str(self.repo/'scripts/deploy-docker-compose.sh'),self.good,'staging'],
                               cwd=self.repo,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            lock=self.repo/'.deployment.lock'
            deadline=time.monotonic()+30
            held=False
            while time.monotonic()<deadline and first.poll() is None:
                with lock.open('a+') as handle:
                    try:
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        fcntl.flock(handle,fcntl.LOCK_UN)
                    except BlockingIOError:
                        held=True
                        break
                time.sleep(.1)
            if not held:
                _, early_stderr=first.communicate(timeout=2) if first.poll() is not None else ('', 'still running')
                raise AssertionError('first deployment never acquired flock; exit='+str(first.returncode)+' stderr='+mask(early_stderr[-700:],self.env))
            inode=lock.stat().st_ino
            second=self.deploy(self.good)
            assert second.returncode!=0 and 'lock unavailable' in second.stderr
            assert lock.stat().st_ino==inode
            stdout,stderr=first.communicate(timeout=780)
            assert first.returncode==0, mask(stderr[-800:],self.env)
            assert lock.stat().st_ino==inode
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
        self.assert_state(self.good,self.baseline)
        self.assert_healthy()
        self.marker()
        print('PASS: real two-process flock contention, stable inode and distinct SHA state')

        for kind in ('migration','bootstrap','backend'):
            bad=self.next_commit(kind)
            result=self.deploy(bad)
            if result.returncode==0 or 'Rollback recovered' not in result.stderr:
                self.diagnose(kind)
                raise AssertionError(f'{kind} failure was not rolled back (exit {result.returncode}): '+mask(result.stderr[-700:],self.env))
            self.assert_state(self.good,self.baseline)
            self.assert_healthy()
            self.marker()
            print(f'PASS: real {kind} failure returned nonzero, restored version/configuration and preserved database')

        bad=self.next_commit('backend')
        result=self.deploy(bad,fail_rollback=True)
        assert result.returncode!=0 and 'ROLLBACK FAILED' in result.stderr, mask(result.stderr[-700:],self.env)
        assert (self.repo/'.deployment-sha').read_text().strip()==self.good
        assert (self.repo/'.previous-deployment-sha').read_text().strip()==self.baseline
        assert run(['sudo','test','-f',str(self.data/'PG_VERSION')],check=False).returncode==0
        print('PASS: injected rollback Docker failure propagates nonzero and retains last successful SHA')

    def cleanup(self):
        errors=[]
        if self.repo.exists():
            try:
                # Only the random Compose project created by this test can be removed.
                self.compose('down','--volumes','--remove-orphans','--timeout','15',check=True,timeout=120)
                for args in (['ps','-aq'],['volume','ls','-q']):
                    check=run(['/usr/bin/docker',*args,'--filter','label=com.docker.compose.project='+self.project])
                    assert not check.stdout.strip(), 'disposable Compose resources remain'
                if run(['sudo','test','-d',str(self.data)],check=False).returncode==0:
                    assert run(['sudo','test','-f',str(self.data/'PG_VERSION')],check=False).returncode==0, 'storage removed by Docker down'
                print('PASS: isolated containers/volumes removed; bind-mounted database persisted until explicit fixture cleanup')
            except Exception as exc:
                errors.append(str(exc))
        # The temp root is created by tempfile under RUNNER_TEMP, never an operator path.
        assert self.tmp.name.startswith(self.project+'-') and self.tmp.parent==Path(os.getenv('RUNNER_TEMP','/tmp'))
        removed=run(['sudo','rm','-rf','--',str(self.tmp)],check=False,timeout=60)
        if removed.returncode:
            errors.append('could not remove disposable fixture root')
        if errors:
            raise AssertionError('; '.join(errors))


def main():
    v=Validation()
    failed=False
    try:
        v.test()
    except Exception as exc:
        failed=True
        print('FAIL: '+mask(str(exc),v.env),file=sys.stderr)
        if v.repo.exists():
            try:
                v.diagnose('unexpected failure')
            except Exception:
                print('Diagnostics unavailable',file=sys.stderr)
    finally:
        try:
            v.cleanup()
        except Exception as exc:
            failed=True
            print('FAIL cleanup: '+mask(str(exc),v.env),file=sys.stderr)
    return 1 if failed else 0


if __name__=='__main__':
    sys.exit(main())
