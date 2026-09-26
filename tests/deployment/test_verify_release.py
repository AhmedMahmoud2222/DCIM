"""Executable release-gate adversarial cases; no live GitHub calls."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / '.github/scripts/verify_release_sha.py'
spec = importlib.util.spec_from_file_location('release_verifier', path)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)
SHA = 'a' * 40


def fixture():
    runs, jobs, checks = [], {}, []
    index = 100
    for label, (workflow_id, workflow_path, names) in v.WORKFLOWS.items():
        run_id = workflow_id + 10000
        runs.append(dict(id=run_id, workflow_id=workflow_id, path=workflow_path,
                         head_sha=SHA, repository={'full_name': v.REPOSITORY},
                         head_repository={'full_name': v.REPOSITORY}, event='push',
                         head_branch='main', run_number=1, run_attempt=1,
                         check_suite_id=run_id+1, status='completed', conclusion='success'))
        jobs[run_id] = []
        for name in names:
            index += 1
            jobs[run_id].append(dict(name=name, run_id=run_id, run_attempt=1, head_sha=SHA,
                                     check_run_url=f'{v.API}/check-runs/{index}',
                                     status='completed', conclusion='success'))
            checks.append(dict(id=index, name=name, head_sha=SHA,
                               status='completed', conclusion='success',
                               check_suite={'id': run_id+1}, app={'slug':'github-actions'}))
    return runs, jobs, checks


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.runs, self.jobs, self.checks = fixture()
        self.total_override = None
        self.fail_page = False
        def mocked(path):
            if path == '/commits/' + SHA:
                return {'sha': SHA}
            if path == '/compare/' + SHA + '...main':
                return {'status': 'ahead', 'merge_base_commit': {'sha': SHA}}
            if path.startswith('/actions/runs?'):
                return {'total_count': len(self.runs), 'workflow_runs': self.runs}
            if '/check-runs?per_page=' in path:
                if self.fail_page and path.endswith('page=2'):
                    raise v.VerificationError('API denied next page')
                number = int(path.split('page=')[-1])
                return {'total_count': self.total_override or len(self.checks),
                        'check_runs': self.checks[(number-1)*100:number*100]}
            if '/attempts/' in path:
                run_id = int(path.split('/runs/')[1].split('/')[0])
                return {'total_count': len(self.jobs[run_id]), 'jobs': self.jobs[run_id]}
            raise AssertionError(path)
        self.patcher = patch.object(v, 'api', side_effect=mocked)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def rejects(self):
        with self.assertRaises(v.VerificationError):
            v.verify(SHA)

    def test_valid(self):
        self.assertEqual(set(v.verify(SHA)), set(v.WORKFLOWS))

    def test_bad_sha(self):
        for value in ('A'*40, 'a'*39, 'a'*41, '"; touch /tmp/nope; #'):
            with self.assertRaises(v.VerificationError):
                v.verify(value)

    def test_unauthorized_commit(self):
        with patch.object(v, 'api', side_effect=lambda path: {'sha': SHA} if path.startswith('/commits/') else {'status':'diverged', 'merge_base_commit': {'sha':'b'*40}}):
            self.rejects()

    def test_wrong_repository_and_workflow(self):
        for key, value in (('path', '.github/workflows/evil.yml'), ('workflow_id', 999),
                           ('head_branch', 'feature'), ('event', 'pull_request')):
            run = self.runs[0]
            old = run[key]
            run[key] = value
            self.rejects()
            run[key] = old
        self.runs[0]['repository']['full_name'] = 'attacker/DCIM'
        self.rejects()

    def test_wrong_app_and_suite(self):
        self.checks[0]['app']['slug'] = 'external'
        self.rejects()
        self.checks[0]['app']['slug'] = 'github-actions'
        self.checks[0]['check_suite']['id'] = 999
        self.rejects()

    def test_duplicate_check_and_job(self):
        self.checks.append(dict(self.checks[0]))
        self.rejects()
        self.checks.pop()
        self.jobs[self.runs[0]['id']].append(dict(self.jobs[self.runs[0]['id']][0]))
        self.rejects()

    def test_newer_failed_run_and_rerun(self):
        newer = dict(self.runs[0], id=900000, run_number=2, conclusion='failure')
        self.runs.append(newer)
        self.rejects()
        self.runs.pop()
        self.runs[0]['run_attempt'] = 2
        self.rejects()  # stale attempt-1 jobs cannot authorize attempt 2

    def test_missing_smoke_skipped_and_failed_ci(self):
        name = 'Compose smoke'
        run_id = self.runs[1]['id']
        job = next(j for j in self.jobs[run_id] if j['name'] == name)
        self.jobs[run_id].remove(job)
        self.rejects()
        self.jobs[run_id].append(job)
        job['conclusion'] = 'skipped'
        self.rejects()
        job['conclusion'] = 'success'
        self.jobs[self.runs[0]['id']][0]['conclusion'] = 'failure'
        self.rejects()

    def test_pagination_and_api_error(self):
        self.total_override = 101
        self.rejects()
        self.total_override = None
        # Force a second check page and deny it.
        self.checks.extend(dict(id=1000+i, name='unrelated') for i in range(92))
        self.fail_page = True
        self.rejects()


if __name__ == '__main__':
    unittest.main()
