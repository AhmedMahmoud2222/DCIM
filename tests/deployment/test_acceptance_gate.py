"""Adversarial executable gate tests; no provider or GitHub credential required."""
import base64
import ast
import copy
import hashlib
import importlib
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
c = importlib.import_module('acceptance_core')
a = importlib.import_module('acceptance_api')
g = importlib.import_module('acceptance_gate')
w = importlib.import_module('acceptance_provider_witness')
guard = importlib.import_module('acceptance_read_guard')
HEAD, BASE = 'a' * 40, 'b' * 40


class Fixture(a.GitHub):
    def __init__(self):
        super().__init__(g.load_policy())
        self.pr = dict(number=211, state='open', merged=False, changed_files=1,
            user=dict(id=self.policy['owner']['id']), head=dict(sha=HEAD, repo=dict(full_name=self.policy['repository'])),
            base=dict(sha=BASE, ref='main', repo=dict(full_name=self.policy['repository'])))
        self.b = c.binding(self.pr, self.policy)
        self.ids = {v['path']: 80000 + i for i, v in enumerate(self.policy['workflows'].values())}
        self.runs, self.jobs, self.checks, self.values, self.artifact_ids = {}, {}, {}, {}, {}
        self.owner_approvals = [dict(environments=[dict(name='acceptance-owner')], state='approved', user=self.policy['owner'])]
        self.thread_values, self.review_values, self.poison = [], [], None
        self.commits = [dict(sha=HEAD, commit=dict(verification=dict(verified=True, reason='valid')))]
        self.compare = dict(status='ahead', behind_by=0, merge_base_commit=dict(sha=BASE), total_commits=1)
        self.witness = dict(id=700, issue_url=self.api_root + '/issues/211',
            body=w.witness_body(self.b, 20, 1),
            user=dict(id=209825114, login='claude[bot]', type='Bot'),
            performed_via_github_app=dict(id=1236702, slug='claude'))
        self.witnesses = {700: self.witness}
        self.posts = 0
        self.owner_run = self.make_run(10, 'owner')
        self.review_run = self.make_run(20, 'review')
        self.gate_run = self.make_run(30, 'gate')
        review = dict(binding=self.b, reviewer='claude', verdict='ACCEPT', findings=[], resolved_finding_ids=[])
        self.add_value(self.review_run, 'review', dict(kind='review', binding=self.b, review=review,
            provider=self.policy['providers']['claude'], action=self.policy['review_action'],
            session_id='session-abc', provider_comment_id=700))
        self.add_value(self.owner_run, 'owner', dict(kind='owner', binding=self.b, actor=self.policy['owner'],
            verdict='AUTHORIZE_INITIAL_SECURITY_REVIEW'))
        self.ci_runs = []
        for i, spec in enumerate(self.policy['ci']):
            run = self.make_run(100 + i, 'ci', spec)
            self.ci_runs.append(run)

    def workflow(self, path):
        return self.ids[path]

    def file(self, path, sha):
        return b'poisoned' if self.poison == (path, sha) else (ROOT / path).read_bytes()

    def make_run(self, ident, kind, spec=None):
        spec = spec or self.policy['workflows'][kind]
        run = dict(id=ident, run_attempt=1, run_number=ident, workflow_id=spec.get('id', self.ids.get(spec['path'])),
            path=spec['path'], event=spec.get('event', 'workflow_run'), head_sha=HEAD if kind == 'ci' else BASE,
            head_branch='feature' if kind == 'ci' else 'main', status='completed', conclusion='success',
            repository=dict(full_name=self.policy['repository']), head_repository=dict(full_name=self.policy['repository']),
            actor=self.policy['owner'].copy(), triggering_actor=self.policy['owner'].copy(), check_suite_id=ident + 1000,
            display_title=('DCIM review PR ' if kind == 'review' else 'DCIM owner PR ') + '211',
            pull_requests=[dict(number=211, head=dict(sha=HEAD), base=dict(sha=BASE))])
        if kind == 'ci': run['event'] = 'pull_request'
        if kind == 'gate': run['event'] = 'workflow_run'
        self.runs[ident] = run
        self.jobs[ident], self.checks[ident] = [], []
        names = ['select', 'acceptance-211'] if kind == 'gate' else list(spec['jobs'])
        if kind == 'ci' and spec['path'].endswith('/opencode-security-validation.yml'):
            tree = ast.parse((ROOT / 'tests/deployment/opencode_validation/mutants.py').read_text())
            inv = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'MUTATIONS' for t in n.targets))
            names += ['Mutant ' + n.elts[0].value for n in inv.elts]
        for i, name in enumerate(names):
            check_id = ident * 100 + i
            self.jobs[ident].append(dict(id=check_id, name=name, run_id=ident, run_attempt=1,
                head_sha=run['head_sha'], status='completed', conclusion='success',
                check_run_url=self.api_root + '/check-runs/' + str(check_id)))
            self.checks[ident].append(dict(id=check_id, name=name, head_sha=run['head_sha'],
                status='completed', conclusion='success', app=self.policy['actions_app'].copy(),
                check_suite=dict(id=run['check_suite_id'])))
        return run

    def add_value(self, run, kind, data):
        value = copy.deepcopy(data)
        value.update(source=g.source(run), policy_digest=c.digest(self.policy))
        key = (run['id'], g.record_name(kind, 211, run['run_attempt']))
        self.values[key], self.artifact_ids[key] = value, run['id'] + 9000

    def artifact(self, run, name):
        key = (run['id'], name)
        c.require(key in self.values, 'missing evidence')
        value = self.values[key]
        c.require(value['source'] == g.source(run) and value['policy_digest'] == c.digest(self.policy), 'artifact provenance mismatch')
        return value, self.artifact_ids[key]

    def threads(self, number):
        return self.thread_values

    def request(self, path, method='GET', body=None):
        if path == '/pulls/211': return self.pr
        if path == '/branches/main': return dict(commit=dict(sha=self.pr['base']['sha']))
        if path.startswith('/compare/'): return self.compare
        if path.startswith('/commits/'):
            if path == '/commits/' + BASE:
                return dict(sha=BASE, commit=dict(verification=dict(verified=True, reason='valid')))
            return next(v for v in self.commits if v['sha'] == path.rsplit('/', 1)[1])
        if path.startswith('/issues/comments/'): return self.witnesses[int(path.rsplit('/', 1)[1])]
        if path == '/issues/211/comments' and method == 'POST':
            self.posts += 1
            self.witness['body'] = body['body']
            return self.witness
        if path.endswith('/approvals'): return self.owner_approvals
        if path.startswith('/actions/runs/'): return self.runs[int(path.rsplit('/', 1)[1])]
        raise AssertionError(path)

    def pages(self, path, key=None, total_key='total_count', limit=2000):
        if path.startswith('/compare/'): return self.commits
        if path.startswith('/actions/runs?'): return self.ci_runs
        if '/attempts/' in path: return self.jobs[int(path.split('/')[3])]
        if path.startswith('/check-suites/'):
            suite = int(path.split('/')[2]); return self.checks[suite - 1000]
        if path.startswith('/commits/'):
            return getattr(self, 'published', []) + [check for run in self.ci_runs for check in self.checks[run['id']]]
        if path.endswith('/reviews'): return self.review_values
        if '/workflows/' in path:
            wid = int(path.split('/')[3])
            return [r for r in self.runs.values() if r['workflow_id'] == wid]
        raise AssertionError(path)

    def publish_fixture(self):
        result = g.evaluate(self, 211)
        value = dict(kind='decision', binding=self.b, source=g.source(self.gate_run),
                     policy_digest=c.digest(self.policy), decision=result)
        self.add_value(self.gate_run, 'acceptance', value)
        self.published = [dict(id=999999, name=c.CHECK, head_sha=HEAD, status='completed', conclusion='success',
            app=self.policy['actions_app'].copy(), external_id='dcim-acceptance:v1:30:1:211:' + c.digest(value))]


class CollectorTests(unittest.TestCase):
    def setUp(self): self.api = Fixture()
    def rejects(self, message=None):
        with self.assertRaises(c.Rejected) as error: g.evaluate(self.api, 211)
        if message: self.assertIn(message, str(error.exception))
    def test_complete_independent_evidence_accepts_without_merging(self):
        result = g.evaluate(self.api, 211)
        self.assertEqual(result['verdict'], 'ACCEPT')
        self.assertFalse(result['merge_enabled']); self.assertFalse(result['deployment_enabled'])
    def test_wrong_provider_app_or_actor_rejects(self):
        self.api.witness['performed_via_github_app']['id'] = 15368
        self.rejects('identity')
        self.api = Fixture(); self.api.witness['user']['id'] = 199175422
        self.rejects('identity')
    def test_ordinary_claude_accept_comment_is_not_authority(self):
        self.api.witness['body'] = 'ACCEPT'
        self.rejects('witness')
        self.api = Fixture(); self.api.values.clear(); self.api.witness['body'] = 'ACCEPT'
        self.rejects('missing evidence')
    def test_self_review_and_author_review_reject(self):
        self.api.policy['implementers']['211'] = 'claude'; self.rejects('self review')
        self.api = Fixture(); self.api.pr['user']['id'] = 209825114; self.rejects('PR author')
    def test_owner_attestation_is_separate_and_cannot_be_invented(self):
        self.api.owner_approvals = []; self.rejects('approval')
        self.api = Fixture(); self.api.owner_approvals[0]['user'] = dict(id=7, login='spoof', type='User')
        self.rejects('owner')
    def test_failed_or_pending_latest_review_never_falls_back(self):
        newer = self.api.make_run(21, 'review'); newer['conclusion'] = 'failure'
        self.rejects('failed/incomplete')
    def test_unrelated_skipped_comment_run_does_not_supersede_review(self):
        newer = self.api.make_run(21, 'review'); newer['conclusion'] = 'skipped'
        selected = g.records(self.api, 'review', 211)
        self.assertNotIn(21, [run['id'] for run in selected])
        self.assertEqual(g.evaluate(self.api, 211)['verdict'], 'ACCEPT')
    def test_wrong_head_base_or_pr_review_rejects(self):
        for field, value in [('head', 'c'*40), ('base', 'c'*40), ('pr', 9)]:
            with self.subTest(field=field):
                self.api = Fixture()
                v = next(v for v in self.api.values.values() if v['kind'] == 'review')
                v['binding'][field] = value
                self.rejects()
    def test_unresolved_threads_or_formal_review_reject(self):
        self.api.thread_values = [dict(id='thread1', isResolved=False)]; self.rejects('threads')
        self.api = Fixture(); self.api.review_values = [dict(id=1, user=dict(id=8), state='CHANGES_REQUESTED')]
        self.rejects('formal review')
    def test_prior_blocking_finding_cannot_disappear(self):
        old = self.api.make_run(19, 'review')
        value = copy.deepcopy(next(v for v in self.api.values.values() if v['kind'] == 'review'))
        value['review'].update(verdict='BLOCK', findings=[dict(id='F1', blocking=True, message='unfixed')])
        value['provider_comment_id'] = 719
        old_witness = copy.deepcopy(self.api.witness)
        old_witness.update(id=719, body=w.witness_body(self.api.b, 19, 1))
        self.api.witnesses[719] = old_witness
        self.api.add_value(old, 'review', value)
        self.rejects('unresolved')
        current = self.api.values[(20, g.record_name('review', 211, 1))]
        current['review']['resolved_finding_ids'] = ['F1']
        self.assertEqual(g.evaluate(self.api, 211)['verdict'], 'ACCEPT')
    def test_complete_signed_range_required(self):
        self.api.commits[0]['commit']['verification']['reason'] = 'unknown_key'; self.rejects('signature')
        self.api = Fixture(); self.api.compare['total_commits'] = 2; self.rejects('incomplete')
        self.api = Fixture(); self.api.commits.append(copy.deepcopy(self.api.commits[0])); self.api.compare['total_commits'] = 2
        self.rejects('duplicate')
    def test_same_named_check_wrong_app_or_suite_rejects(self):
        self.api.checks[100][0]['app']['id'] = 999; self.rejects('App')
        self.api = Fixture(); self.api.checks[100][0]['check_suite']['id'] = 999; self.rejects('suite')
    def test_aggregate_success_does_not_replace_every_mutant_job(self):
        self.api.jobs[103][-1]['conclusion'] = 'skipped'
        self.rejects('failed/stale')
    def test_failed_latest_ci_rerun_and_stale_base_reject(self):
        self.api.ci_runs[0]['run_attempt'] = 2; self.rejects('attempt')
        self.api = Fixture(); self.api.ci_runs[0]['pull_requests'][0]['base']['sha'] = 'c'*40
        self.rejects('another PR/base')
    def test_compromised_workflow_source_rejects(self):
        path = self.api.policy['workflows']['review']['path']
        self.api.poison = (path, BASE); self.rejects('provenance')
        self.api = Fixture(); self.api.poison = (self.api.policy['ci'][0]['path'], HEAD)
        self.rejects('changed its own CI')
    def test_duplicate_reviews_checks_and_evidence_reject(self):
        self.api.checks[100].append(copy.deepcopy(self.api.checks[100][0])); self.rejects('duplicate')
        self.api = Fixture(); value = self.api.values[(20, g.record_name('review', 211, 1))]
        value['review']['findings'] = [dict(id='F1', blocking=False, message='a')]*2
        self.rejects('duplicate')
    def test_source_workflow_actor_ref_and_trigger_reject(self):
        for field, value in [('head_branch', 'feature'), ('event', 'pull_request'), ('head_sha', HEAD),
                              ('workflow_id', 999), ('path', '.github/workflows/forged.yml')]:
            with self.subTest(field=field):
                self.api = Fixture(); self.api.review_run[field] = value; self.rejects()
        self.api = Fixture(); self.api.review_run['actor']['id'] = 9; self.rejects('actor')
    def test_published_check_requires_provenance_and_live_evidence(self):
        self.api.publish_fixture()
        self.assertEqual(g.verify_check(self.api, 211)['verdict'], 'ACCEPT')
        self.api.ci_runs[0]['conclusion'] = 'failure'
        with self.assertRaises(c.Rejected): g.verify_check(self.api, 211)
    def test_forged_actions_check_with_correct_name_is_rejected(self):
        self.api.publish_fixture(); self.api.published[0]['external_id'] = 'dcim-acceptance:v1:30:1:211:' + '0'*64
        with self.assertRaisesRegex(c.Rejected, 'forged'): g.verify_check(self.api, 211)
        self.api = Fixture(); self.api.publish_fixture(); self.api.gate_run['path'] = '.github/workflows/evil.yml'
        with self.assertRaises(c.Rejected): g.verify_check(self.api, 211)
    def test_duplicate_published_evidence_rejects(self):
        self.api.publish_fixture(); duplicate = copy.deepcopy(self.api.published[0]); duplicate['id'] += 1
        self.api.published.append(duplicate)
        with self.assertRaisesRegex(c.Rejected, 'duplicate'): g.verify_check(self.api, 211)
    def test_newer_gate_execution_invalidates_old_accept(self):
        self.api.publish_fixture(); newer = self.api.make_run(31, 'gate'); newer['conclusion'] = 'failure'
        with self.assertRaisesRegex(c.Rejected, 'supersedes'): g.verify_check(self.api, 211)


class ApiAndSchemaTests(unittest.TestCase):
    def test_context_is_lossless_and_readable_in_bounded_lines(self):
        api = Fixture()
        review = copy.deepcopy(api.values[(20, g.record_name('review', 211, 1))]['review'])
        review['findings'] = [dict(id='F1', blocking=True, message='x' * 4000)]
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                g.prepare_context(api.b, [review], [dict(filename='x.py', patch='@@ hunk\n+a\n-b')])
                raw = Path('review-input/context.json').read_text()
                self.assertLessEqual(max(map(len, raw.splitlines())), 1800)
                value = json.loads(raw)
                self.assertEqual(value['files'][0]['patch_lines'], ['@@ hunk', '+a', '-b'])
                self.assertEqual(''.join(value['prior_reviews'][0]['findings'][0]['message_parts']), 'x' * 4000)
                with self.assertRaises(c.Rejected):
                    g.prepare_context(api.b, [], [dict(filename='x', patch='+' + 'x' * 2000)])
            finally:
                os.chdir(old)
    def test_complete_findings_are_split_below_github_comment_limit(self):
        api = Fixture()
        value = copy.deepcopy(api.values[(20, g.record_name('review', 211, 1))])
        value['review']['findings'] = [dict(id='F'+str(i), blocking=True, message='x'*4000) for i in range(100)]
        comments = g.finding_comments(value)
        self.assertGreater(len(comments), 1)
        items = []
        for body in comments:
            self.assertLess(len(body.encode()), 65536)
            data = json.loads(body.split('```json\n', 1)[1].rsplit('\n```', 1)[0])
            items.extend(data['items'])
        self.assertEqual(len(items), 100)
    def test_native_session_hook_creates_one_authenticated_witness_without_model_tools(self):
        api = Fixture()
        env = dict(GITHUB_REPOSITORY=api.policy['repository'], GITHUB_REF='refs/heads/main',
            GITHUB_WORKFLOW_REF=api.policy['repository'] + '/.github/workflows/acceptance-review.yml@refs/heads/main',
            GITHUB_WORKFLOW_SHA=BASE, GITHUB_RUN_ID='20', GITHUB_RUN_ATTEMPT='1', REVIEW_BINDING=json.dumps(api.b))
        with tempfile.TemporaryDirectory() as tmp:
            env['RUNNER_TEMP'] = tmp
            with patch.dict(os.environ, env), patch.object(w, 'GitHub', return_value=api), patch.object(w, 'clear_git_credentials'):
                self.assertEqual(w.main(), 0)
                self.assertEqual(w.main(), 0)
            self.assertEqual(api.posts, 1)
            record = json.loads((Path(tmp) / 'acceptance-witness-20-1.json').read_text())
            self.assertEqual(record, {'id':700})
    def test_native_hook_rejects_actions_token_in_place_of_provider_token(self):
        api = Fixture(); api.witness['performed_via_github_app']['id'] = 15368
        env = dict(GITHUB_REPOSITORY=api.policy['repository'], GITHUB_REF='refs/heads/main',
            GITHUB_WORKFLOW_REF=api.policy['repository'] + '/.github/workflows/acceptance-review.yml@refs/heads/main',
            GITHUB_WORKFLOW_SHA=BASE, GITHUB_RUN_ID='20', GITHUB_RUN_ATTEMPT='1', REVIEW_BINDING=json.dumps(api.b))
        with tempfile.TemporaryDirectory() as tmp:
            env['RUNNER_TEMP'] = tmp
            with patch.dict(os.environ, env), patch.object(w, 'GitHub', return_value=api), patch.object(w, 'clear_git_credentials'), self.assertRaises(c.Rejected):
                w.main()
            self.assertEqual((Path(tmp) / 'acceptance-witness-20-1.json').read_text(), '{}')
    def test_explicit_owner_checkpoint_preserves_findings_and_skips_expired_history(self):
        api = Fixture()
        checkpoint = dict(version=1, pr=211, workflow_id=api.workflow(api.policy['workflows']['review']['path']),
            through_run_number=19, through_run_id=19, history_digest='c'*64,
            outstanding_findings=[dict(id='old-F1', blocking=True, message='still open')])
        owner_commit = dict(author=dict(id=285010264), committer=dict(id=19864447),
                            commit=dict(verification=dict(verified=True, reason='valid')))
        with tempfile.TemporaryDirectory() as tmp:
            api.root = Path(tmp)
            shutil.copytree(ROOT / '.github', api.root / '.github',
                            ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
            path = api.root / '.github/acceptance/checkpoints/211.json'
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(checkpoint))
            old_file = api.file
            api.file = lambda p, sha: path.read_bytes() if p.endswith('/211.json') else old_file(p, sha)
            original = api.request
            api.request = lambda p, *args: [owner_commit] if p.startswith('/commits?path=') else original(p, *args)
            old = api.make_run(19, 'review')  # No artifact: expired history below signed boundary.
            outstanding, history = g.previous_findings(api, 211)
            self.assertEqual(outstanding, {'old-F1'})
            self.assertEqual(history[0]['findings'][0]['message'], 'still open')
            api.poison = ('.github/acceptance/policy.json', BASE)
            self.assertEqual(g.checkpoint(api, 211)[0], {'old-F1'})
            api.poison = None
            owner_commit['author']['id'] = 9
            with self.assertRaisesRegex(c.Rejected, 'owner-signed'): g.previous_findings(api, 211)
            owner_commit['author']['id'] = 285010264
            checkpoint['through_run_number'] = 999999
            path.write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(c.Rejected, 'boundary'): g.previous_findings(api, 211)
    def test_exclusive_model_read_boundary_denies_credentials_and_other_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'review-input').mkdir()
            (root / 'review-input/context.json').write_text('{}')
            self.assertTrue(guard.allowed(dict(tool_name='Read', tool_input=dict(file_path='review-input/context.json')), root))
            for path in ('.git/config', '/proc/self/environ', '/home/runner/.claude/.credentials.json',
                         '/tmp/credential', 'review-input/../.git/config'):
                self.assertFalse(guard.allowed(dict(tool_name='Read', tool_input=dict(file_path=path)), root))
            for tool in ('Bash', 'Write', 'WebFetch', 'mcp__github__create_comment'):
                self.assertFalse(guard.allowed(dict(tool_name=tool, tool_input={}), root))
            (root / 'review-input/context.json').unlink()
            (root / 'secret').write_text('SYNTHETIC')
            (root / 'review-input/context.json').symlink_to(root / 'secret')
            self.assertFalse(guard.allowed(dict(tool_name='Read', tool_input=dict(file_path='review-input/context.json')), root))
    def test_startup_removes_native_persisted_git_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run(['git', '-C', tmp, 'config', 'remote.origin.url', 'https://x-access-token:SYNTHETIC@github.com/AhmedMahmoud2222/DCIM.git'], check=True)
            subprocess.run(['git', '-C', tmp, 'config', 'http.https://github.com/.extraheader', 'AUTHORIZATION: SYNTHETIC'], check=True)
            w.clear_git_credentials(tmp, 'AhmedMahmoud2222/DCIM')
            url = subprocess.check_output(['git', '-C', tmp, 'remote', 'get-url', 'origin'], text=True).strip()
            self.assertEqual(url, 'https://github.com/AhmedMahmoud2222/DCIM.git')
            self.assertNotEqual(subprocess.run(['git', '-C', tmp, 'config', '--get', 'http.https://github.com/.extraheader'], capture_output=True).returncode, 0)
    def test_duplicate_keys_nonfinite_json_and_extra_output_fields_reject(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', 'not json'):
            with self.assertRaises(c.Rejected): c.decode(raw)
        value = Fixture().values[(20, g.record_name('review', 211, 1))]['review']
        value['comment_says_approved'] = True
        with self.assertRaises(c.Rejected): c.structured_review(value, Fixture().b, 'claude')
    def test_source_revision_and_completion_checks_are_independently_required(self):
        api = Fixture(); spec = api.policy['workflows']['review']; run = copy.deepcopy(api.review_run)
        run['head_sha'] = HEAD
        with self.assertRaises(c.Rejected): c.source_run(run, spec, run['workflow_id'], api.policy, BASE)
        run = copy.deepcopy(api.review_run); run['conclusion'] = 'failure'
        with self.assertRaises(c.Rejected): c.source_run(run, spec, run['workflow_id'], api.policy, BASE)
    def test_structured_output_rejects_wrong_binding_before_other_evidence(self):
        api = Fixture()
        value = copy.deepcopy(api.values[(20, g.record_name('review', 211, 1))]['review'])
        value['binding']['head'] = 'c' * 40
        with self.assertRaises(c.Rejected): c.structured_review(value, api.b, 'claude')
    def test_default_branch_runtime_guard_rejects_feature_execution(self):
        api = Fixture()
        env = dict(GITHUB_REPOSITORY=api.policy['repository'], GITHUB_REF='refs/heads/feature',
                   GITHUB_WORKFLOW_REF=api.policy['repository'] + '/.github/workflows/acceptance-gate.yml@refs/heads/feature',
                   GITHUB_WORKFLOW_SHA=BASE)
        with patch.dict(os.environ, env), self.assertRaises(c.Rejected):
            api.runtime('.github/workflows/acceptance-gate.yml')
    def test_artifact_digest_redirect_and_duplicate_archive_reject(self):
        fixture = Fixture(); run = fixture.review_run
        record = fixture.values[(20, g.record_name('review', 211, 1))]
        def archive(duplicate=False):
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as z:
                z.writestr('evidence.json', json.dumps(record))
                if duplicate: z.writestr('../escape', 'bad')
            return raw.getvalue()
        raw = archive()
        meta = dict(id=9, name='sealed', expired=False, size_in_bytes=len(raw),
                    workflow_run=dict(id=20, head_sha=BASE), digest='sha256:' + hashlib.sha256(raw).hexdigest())
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        class Opener:
            def __init__(self): self.requests = []
            def open(self, req, timeout):
                self.requests.append(req)
                if len(self.requests) % 2:
                    raise urllib.error.HTTPError(req.full_url, 302, 'redirect', {'Location':'https://fixture.blob.core.windows.net/a?sig=synthetic'}, None)
                return Response(raw)
        api = a.GitHub(fixture.policy)
        api.pages = lambda *args: [meta]
        api.opener = Opener()
        with patch.dict(os.environ, {'GITHUB_TOKEN':'SYNTHETIC'}):
            result, _ = api.artifact(run, 'sealed')
        self.assertEqual(result, record)
        self.assertIsNone(api.opener.requests[1].get_header('Authorization'))
        meta['digest'] = 'sha256:' + '0'*64
        with self.assertRaisesRegex(c.Rejected, 'integrity'): api.artifact(run, 'sealed')
        raw = archive(True); meta['digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(c.Rejected, 'archive'): api.artifact(run, 'sealed')
        raw = archive(); meta['digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
        api.pages = lambda *args: [meta, meta]
        with self.assertRaisesRegex(c.Rejected, 'duplicate'): api.artifact(run, 'sealed')
    def test_workflows_never_check_out_candidate_or_expose_write_token_to_model(self):
        workflows = {}
        for kind in ('review', 'owner', 'gate'):
            path = ROOT / g.load_policy()['workflows'][kind]['path']
            text = path.read_text(); wf = yaml.safe_load(text); workflows[kind] = wf
            triggers = wf.get(True, wf.get('on'))
            self.assertNotIn('pull_request_target', triggers)
            self.assertNotIn('pull_request', triggers)
            self.assertEqual(wf['permissions'], {})
            for job in wf['jobs'].values():
                for step in job['steps']:
                    if step.get('uses', '').startswith('actions/checkout@'):
                        self.assertEqual(step['with']['ref'], '${{ github.workflow_sha }}')
                        self.assertFalse(step['with']['persist-credentials'])
                    self.assertNotIn('${{ inputs.', step.get('run', ''))
                    self.assertNotIn('${{ github.event.', step.get('run', ''))
        review = workflows['review']['jobs']['review']
        self.assertNotIn('write', [v for k, v in review['permissions'].items() if k != 'id-token'])
        action = next(s for s in review['steps'] if s.get('id') == 'claude')
        self.assertEqual(action['uses'], g.load_policy()['review_action'])
        self.assertFalse(action['with']['show_full_output'])
        settings = json.loads(action['with']['settings'])
        self.assertEqual(settings['hooks']['SessionStart'][0]['hooks'][0]['command'],
                         'python3 .github/scripts/acceptance_provider_witness.py')
        self.assertNotIn('secrets.', json.dumps(workflows['gate']))
        self.assertNotIn('checks', workflows['review']['jobs']['seal']['permissions'].get('checks', ''))
    def test_publisher_protects_all_acceptance_controls(self):
        publish = importlib.import_module('opencode_publish')
        for path in ('.github/acceptance/policy.json', '.github/scripts/acceptance_core.py',
                     '.github/scripts/acceptance_api.py', '.github/scripts/acceptance_gate.py',
                     '.github/scripts/acceptance_provider_witness.py',
                     'tests/deployment/test_acceptance_gate.py'):
            self.assertTrue(publish.is_protected_path(path), path)
