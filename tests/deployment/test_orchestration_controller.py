"""Security and recovery tests of the actual collaboration reconciler."""
import base64
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import orchestration_controller as c
from acceptance_core import Rejected, digest
from acceptance_api import TransientError, MAX_REQUEST_ATTEMPTS
from agent_authority import comment_binding


class API:
    def __init__(self, policy):
        self.policy = policy
        self.api_root = 'https://api.github.com/repos/' + policy['repository']
        self.root = ROOT
        self.pr = dict(number=211, state='open', user={'id': 1}, head={'sha': 'a'*40, 'repo': {'full_name': policy['repository']}}, base={'sha': 'b'*40, 'ref': 'main', 'repo': {'full_name': policy['repository']}})
        self.root_comment = dict(id=1, body='/oc review\nDCIM-Head: ' + 'a'*40,
                                 user=dict(policy['owner']), author_association='OWNER', created_at='2026-10-27T00:00:00Z', updated_at='2026-10-27T00:00:00Z')
        self.comments = [self.root_comment]
        self.posts = []
        self.raise_post = False

    def request(self, path, method='GET', body=None):
        if path == '/pulls/211':
            return deepcopy(self.pr)
        if method == 'POST':
            if self.raise_post:
                raise Rejected('ambiguous delivery')
            self.posts.append(body)
            item = dict(id=19+len(self.posts), body=body['body'], user={k:v for k,v in self.policy['issuer'].items() if k in ('id', 'login')},
                        performed_via_github_app={'id': self.policy['issuer']['app_id'], 'slug': self.policy['issuer']['app_slug']}, created_at='now', updated_at='now')
            item['user']['type'] = 'Bot'
            self.comments.append(item)
            return item
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path.startswith('/issues/'):
            return deepcopy(self.comments)
        if path.startswith('/actions/workflows/'):
            return [{'id': 77, 'status': 'completed', 'conclusion': 'success',
                     'workflow_id': 88, 'head_branch': 'main', 'event': 'issue_comment',
                     'head_sha': 'a' * 40, 'repository': {'full_name': self.policy['repository']},
                     'run_attempt': 1, 'check_suite_id': 9}]
        raise AssertionError(path)


class MemoryStore:
    def __init__(self):
        self.value = None
        self.writes = 0
        self.conflict = False

    def save(self, value, sha):
        if self.conflict:
            raise Rejected('CAS conflict')
        self.value = deepcopy(value)
        self.value['revision'] += 1
        self.writes += 1
        return deepcopy(self.value), str(self.writes)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT/c.POLICY).read_text())
        self.policy['issuer'] = dict(id=100, login='native[bot]', app_id=200, app_slug='native')
        self.policy['providers']['claude']['proof'] = dict(run_id=77, workflow_id=88, workflow_path='.github/workflows/claude.yml', jobs=['review'])
        self.api = API(self.policy)
        command = dict(id=2, body='/collaborate 1', user=dict(self.policy['owner']), author_association='OWNER', created_at='now', updated_at='now')
        self.task = c.new_task(self.api, self.policy, 211, command, self.api.comments, 100)
        self.state = dict(version=1, revision=0, tasks={self.task['id']: self.task})
        self.store = MemoryStore()

    def run_task(self, state=None, now=110):
        with patch.object(c, 'choose', return_value='claude'):
            return c.reconcile(self.api, self.store, self.policy, state or self.state, '0', self.task['id'], now)

    def test_outbox_is_signed_and_saved_before_delivery(self):
        original = self.api.request
        def request(path, method='GET', body=None):
            if method == 'POST':
                self.assertEqual(self.store.value['tasks'][self.task['id']]['deliveries'][0]['status'], 'RESERVED')
            return original(path, method, body)
        self.api.request = request
        value, _ = self.run_task()
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['state'], 'INDEPENDENT_REVIEW')
        self.assertEqual(task['deliveries'][0]['comment_id'], 20)
        self.assertEqual(len(self.api.posts), 1)

    def test_cas_conflict_prevents_dispatch(self):
        self.store.conflict = True
        with self.assertRaises(Rejected): self.run_task()
        self.assertEqual(self.api.posts, [])

    def test_ambiguous_post_is_not_retried(self):
        self.api.raise_post = True
        # An ambiguous/definitive POST failure escalates only this task;
        # it is not retried and no Rejected escapes reconcile.
        value, _ = self.run_task()
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
        self.assertEqual(self.api.posts, [])
        # A subsequent run leaves the terminal task untouched.
        self.api.raise_post = False
        value2, _ = self.run_task(value)
        self.assertEqual(value2['tasks'][self.task['id']]['state'], 'ESCALATED')
        self.assertEqual(self.api.posts, [])

    def test_interrupted_success_recovers_exactly_once(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        d = reserved['tasks'][self.task['id']]['deliveries'][0]
        d['status'] = 'RESERVED'
        d.pop('comment_id')
        value, _ = self.run_task(reserved)
        self.assertEqual(value['tasks'][self.task['id']]['deliveries'][0]['comment_id'], 20)
        self.assertEqual(len(self.api.posts), 1)

    def test_duplicate_delivery_fails_closed(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        self.api.comments.append(dict(self.api.comments[-1], id=21))
        value, _ = self.run_task(reserved)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
        self.assertEqual(len(self.api.posts), 1)

    def test_spoofed_app_cannot_recover(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        self.api.comments[-1]['performed_via_github_app']['id'] = 999
        value, _ = self.run_task(reserved)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')

    def test_spoofed_copy_does_not_shadow_authenticated_delivery(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        spoof = deepcopy(self.api.comments[-1])
        spoof['id'] = 19
        spoof['performed_via_github_app']['id'] = 999
        self.api.comments.insert(0, spoof)
        value, _ = self.run_task(reserved)
        self.assertEqual(value['tasks'][self.task['id']]['deliveries'][0]['comment_id'], 20)
        self.assertEqual(len(self.api.posts), 1)

    def test_edited_delivery_cannot_recover(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        self.api.comments[-1]['updated_at'] = 'later'
        value, _ = self.run_task(reserved)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')

    def test_conflicting_first_root_does_not_stop_independent_second_root(self):
        for mode in ('missing', 'conflict'):
            with self.subTest(mode=mode):
                self.setUp()
                value, blob = self.run_task()
                value['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
                if mode == 'missing': self.api.comments.pop()
                else: self.api.comments.append(dict(self.api.comments[-1], id=22))
                root2 = deepcopy(self.api.root_comment);root2['id'] = 3
                self.api.comments.append(root2)
                command = dict(id=4, body='/collaborate 3', user=dict(self.policy['owner']),
                               author_association='OWNER', created_at='now', updated_at='now')
                second = c.new_task(self.api, self.policy, 211, command, self.api.comments, 100)
                value['tasks'][second['id']] = second
                with patch.object(c, 'choose', return_value='claude'):
                    for task_id in sorted(value['tasks']):
                        value, blob = c.reconcile(self.api,self.store,self.policy,value,blob,task_id,110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
                self.assertEqual(value['tasks'][second['id']]['state'], 'INDEPENDENT_REVIEW')
                self.assertEqual(len(self.api.posts), 2)

    def test_main_advance_preserves_execution_but_blocks_stale_acceptance(self):
        value, _ = self.run_task()
        self.api.pr['base']['sha'] = 'c'*40
        result = dict(result='SUCCESS', binding=deepcopy(self.task['binding']), source_sha='b'*40)
        with patch.object(c, 'authenticated_execution', return_value=result), patch.object(c, 'verify_check') as gate:
            value, _ = self.run_task(value)
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['state'], 'BLOCKED')
        self.assertEqual(task['deliveries'][-1]['status'], 'COMPLETED')
        self.assertEqual(task['deliveries'][-1]['execution_evidence']['binding']['base'], 'b'*40)
        gate.assert_not_called()

    def test_changed_head_or_base_blocks(self):
        for side in ('head', 'base'):
            with self.subTest(side=side):
                self.api.pr[side]['sha'] = 'c'*40
                value, _ = self.run_task()
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'BLOCKED')
                self.assertEqual(self.api.posts, [])
                self.api.pr[side]['sha'] = ('a' if side == 'head' else 'b')*40

    def test_edited_root_blocks_and_reactions_do_not(self):
        self.api.root_comment['reactions'] = {'total_count': 10}
        self.run_task()
        self.assertEqual(len(self.api.posts), 1)
        self.api.root_comment['updated_at'] = 'later'
        value, _ = self.run_task()
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'BLOCKED')

    def test_deadline_prevents_loops(self):
        value, _ = self.run_task(now=3700)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
        self.assertEqual(self.api.posts, [])

    def test_delegation_budget_exhaustion(self):
        self.task['deliveries'] = [dict(status='COMPLETED', comment_id=8) for _ in range(4)]
        value, _ = self.run_task()
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
        self.assertEqual(self.api.posts, [])

    def test_no_provider_ready_escalates(self):
        with patch.object(c, 'choose', return_value=None):
            value, _ = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')

    def test_self_review_and_provider_alias_are_ineligible(self):
        for provider in self.policy['providers'].values(): provider['enabled'] = True
        self.policy['providers']['opencode']['identity'] = dict(self.policy['providers']['codex']['identity'])
        with patch.object(c, 'authenticated_execution', side_effect=Rejected('no actual proof')):
            self.assertIsNone(c.choose(self.api, self.policy, self.task, 110))

    def test_missing_proof_cannot_establish_availability(self):
        self.policy['providers']['claude']['enabled'] = True
        self.policy['providers']['claude']['proof'] = None
        self.assertIsNone(c.choose(self.api, self.policy, self.task, 110))

    def test_invalid_root_identity_and_edited_owner(self):
        command = dict(id=2, body='/collaborate 1', user=dict(self.policy['owner']), author_association='OWNER', created_at='now', updated_at='now')
        command['user']['id'] = 999
        with self.assertRaises(Rejected): c.new_task(self.api, self.policy, 211, command, self.api.comments, 100)
        command['user'] = dict(self.policy['owner'])
        command['updated_at'] = 'later'
        with self.assertRaises(Rejected): c.new_task(self.api, self.policy, 211, command, self.api.comments, 100)

    def test_remediation_not_granted_by_review_root(self):
        self.task['mode'] = 'implement'
        with self.assertRaises(Rejected): c.command_body(self.task, 'claude', 'nonce', self.policy)

    def test_copilot_has_no_invented_adapter(self):
        with self.assertRaises(Rejected): c.command_body(self.task, 'copilot', 'nonce', self.policy)

    def test_terminal_replay_produces_no_delivery(self):
        self.task['state'] = 'ACCEPTED'
        value, _ = self.run_task()
        self.assertEqual(value, self.state)
        self.assertEqual(self.api.posts, [])

    def test_failed_provider_result_routes_bounded_fallback(self):
        value, _ = self.run_task()
        with patch.object(c, 'authenticated_execution', return_value={'result': 'FAILED'}):
            value, _ = self.run_task(value)
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['state'], 'ASSIGNED')
        self.assertEqual(task['deliveries'][-1]['status'], 'COMPLETED')

    def test_success_without_real_acceptance_is_blocked(self):
        value, _ = self.run_task()
        with patch.object(c, 'authenticated_execution', return_value={'result': 'SUCCESS'}), patch.object(c, 'verify_check', side_effect=Rejected('missing CI')):
            value, _ = self.run_task(value)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'BLOCKED')

    def test_actual_store_rejects_unsigned_commits(self):
        class BadAPI:
            def request(self, path, method='GET', body=None):
                if path.startswith('/branches/'): return {'protected':True,'commit': {'sha':'a'*40}}
                return {'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}}
        with self.assertRaises(Rejected): c.Store(BadAPI(), self.policy).read()

    def test_actual_store_cas_conflict_does_not_retry(self):
        calls=[]
        class ConflictAPI:
            def request(self, path, method='GET', body=None):
                calls.append((path, method))
                raise Rejected('HTTP 409')
        with self.assertRaises(Rejected): c.Store(ConflictAPI(), self.policy).save(self.state, 'oldblob')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'PUT')

    def test_actual_store_signature_checked_after_write(self):
        class UnsignedAPI:
            def request(self, path, method='GET', body=None):
                if method == 'PUT': return {'commit': {'sha':'a'*40}, 'content': {'sha':'blob'}}
                return {'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}}
        with self.assertRaises(Rejected): c.Store(UnsignedAPI(), self.policy).save(self.state, 'oldblob')


    def native_fixture(self):
        now = c.timestamp('2026-10-27T00:00:00Z')
        proof = self.policy['providers']['claude']['proof']
        repo = self.policy['repository']
        run = dict(id=77, workflow_id=88, path='.github/workflows/claude.yml', head_sha='b'*40, head_branch='main',
                   event='issue_comment', status='completed', conclusion='success', updated_at='2026-10-27T00:00:00Z',
                   repository={'full_name':repo}, head_repository={'full_name':repo}, run_attempt=1, check_suite_id=9)
        job = dict(name='review', id=12, check_run_url=self.api.api_root+'/check-runs/13', run_id=77, run_attempt=1,
                   head_sha='b'*40, status='completed', conclusion='success')
        check = dict(id=13, name='review', head_sha='b'*40, status='completed', conclusion='success',
                     app={'id':15368,'slug':'github-actions'}, check_suite={'id':9})
        provider = self.policy['providers']['claude']['identity']
        comment = dict(id=44, user={'id':provider['id'],'login':provider['login'],'type':'Bot'},
                       performed_via_github_app={'id':provider['app_id'],'slug':provider['app_slug']},
                       issue_url=self.api.api_root+'/issues/211',created_at='now',updated_at='now')
        result = dict(provider='claude',mode='review',result='SUCCESS',binding=self.task['binding'],provider_comment_id=44,
                      source_sha='b'*40,run_id=77,attempt=1,policy_digest=digest(self.policy))
        self.api.request = lambda path, *args, **kwargs: deepcopy(dict(sha=path.rsplit('/',1)[-1],commit={'verification':{'verified':True,'reason':'valid'}}) if path.startswith('/commits/') else run if path.startswith('/actions/runs/') else comment)
        self.api.pages = lambda path, key=None: deepcopy([job] if key == 'jobs' else [check])
        self.api.trusted_file = lambda *args: None
        self.api.artifact = lambda *args: (deepcopy(result), {})
        return proof, run, job, check, comment, result, now

    def test_native_execution_positive_and_forged_actions_app(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        with patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='b'*40):
            self.assertEqual(c.authenticated_execution(self.api, proof, self.policy, 'claude', now), result)
            check['app']['id'] = 999
            with self.assertRaises(Rejected): c.authenticated_execution(self.api, proof, self.policy, 'claude', now)

    def test_native_result_survives_benign_main_advance_with_original_binding(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        original = self.api.request
        def request(path, *args, **kwargs):
            if path.startswith('/compare/'):
                return dict(base_commit={'sha':'b'*40},merge_base_commit={'sha':'b'*40},status='ahead',behind_by=0,total_commits=1,commits=[{'sha':'c'*40}])
            if path.startswith('/git/trees/'):
                return dict(truncated=False,tree=[dict(path=proof['workflow_path'],mode='100644',type='blob',sha='d'*40)])
            return original(path,*args,**kwargs)
        self.api.request=request
        self.api.file=lambda *args:json.dumps(self.policy).encode()
        with patch.dict(c.os.environ,GITHUB_WORKFLOW_SHA='c'*40):
            self.assertEqual(c.authenticated_execution(self.api,proof,self.policy,'claude',now),result)
            self.policy['execution_provenance']['revoked_revisions']=['b'*40]
            with self.assertRaisesRegex(Rejected,'revoked'):
                c.authenticated_execution(self.api,proof,self.policy,'claude',now)

    def test_native_wrong_run_source_and_expired_proof_rejected(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        with patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='b'*40):
            for key, value in [('head_sha','c'*40),('path','untrusted'),('workflow_id',99),('event','pull_request'),('head_branch','feature')]:
                original = run[key]
                run[key] = value
                with self.subTest(key=key), self.assertRaises(Rejected): c.authenticated_execution(self.api, proof, self.policy, 'claude', now)
                run[key] = original
            with self.assertRaises(Rejected): c.authenticated_execution(self.api, proof, self.policy, 'claude', now+3601)

    def test_native_result_identity_and_stale_binding_rejected(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        result.update(task_id=self.task['id'],delivery_id='nonce')
        with patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='b'*40):
            comment['performed_via_github_app']['id'] = 999
            with self.assertRaises(Rejected): c.authenticated_execution(self.api, proof, self.policy, 'claude', now, self.task, {'id':'nonce'})
            comment['performed_via_github_app']['id'] = self.policy['providers']['claude']['identity']['app_id']
            result['binding'] = dict(result['binding'],head='f'*40)
            with self.assertRaises(Rejected): c.authenticated_execution(self.api, proof, self.policy, 'claude', now, self.task, {'id':'nonce'})


    def test_last_budget_slot_reserved_delivery_recovers_before_budget_check(self):
        value, _ = self.run_task()
        task = value['tasks'][self.task['id']]
        delivered = deepcopy(task['deliveries'][0])
        delivered['status'] = 'RESERVED'
        delivered.pop('comment_id')
        task['deliveries'] = [dict(status='COMPLETED',comment_id=10+i) for i in range(3)] + [delivered]
        value, _ = self.run_task(value)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'INDEPENDENT_REVIEW')
        self.assertEqual(value['tasks'][self.task['id']]['deliveries'][-1]['comment_id'], 20)
        self.assertEqual(len(self.api.posts), 1)

    def pending_fixture(self):
        now = c.timestamp('2026-10-27T00:01:00Z')
        command = dict(id=2, body='/collaborate 1',user=dict(self.policy['owner']),author_association='OWNER',
                       created_at='2026-10-27T00:00:00Z',updated_at='2026-10-27T00:00:00Z')
        self.api.comments.append(command)
        empty = dict(version=1,revision=0,tasks={})
        return now, command, empty

    def test_schedule_discovers_never_journaled_owner_command_and_replay_preserves_deadline(self):
        now, command, state = self.pending_fixture()
        value, blob = c.ingest_pending(self.api,self.store,self.policy,state,'0',now)
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['event'],2)
        self.assertEqual(task['created_at'],now-60)
        self.assertEqual(len(value['tasks']),1)
        writes = self.store.writes
        command['id'] = 3  # A duplicate owner event references same durable root.
        repeated, _ = c.ingest_pending(self.api,self.store,self.policy,value,blob,now+30)
        self.assertEqual(repeated,value)
        self.assertEqual(self.store.writes,writes)

    def test_schedule_does_not_ingest_unsupported_or_stale_or_edited_commands(self):
        now, command, state = self.pending_fixture()
        original = deepcopy(command)
        for change in ({'user':dict(command['user'],id=999)}, {'updated_at':'later'}, {'created_at':'2026-10-26T00:00:00Z','updated_at':'2026-10-26T00:00:00Z'}):
            command.clear()
            command.update(original,**change)
            value, _ = c.ingest_pending(self.api,self.store,self.policy,state,'0',now)
            self.assertEqual(value['tasks'],{})
            self.assertEqual(self.store.writes,0)

    def test_pending_scan_cas_conflict_is_not_retried(self):
        now, command, state = self.pending_fixture()
        self.store.conflict = True
        with self.assertRaises(Rejected): c.ingest_pending(self.api,self.store,self.policy,state,'0',now)
        self.assertEqual(self.api.posts,[])

    def test_store_rejects_main_or_other_state_branch_and_arbitrary_path(self):
        for change in ({'state_branch':'main'}, {'state_branch':'automation/other'}, {'state_path':'README.md'}, {'default_branch':'automation/collaboration-state'}):
            with self.subTest(change=change), self.assertRaises(Rejected): c.Store(self.api,dict(self.policy,**change))

    def test_native_artifact_execution_and_policy_bindings_are_required(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        with patch.dict(c.os.environ,GITHUB_WORKFLOW_SHA='b'*40):
            for key, wrong in [('source_sha','f'*40),('run_id',78),('attempt',2),('policy_digest','f'*64)]:
                original = result[key]
                result[key] = wrong
                with self.subTest(key=key), self.assertRaises(Rejected): c.authenticated_execution(self.api,proof,self.policy,'claude',now)
                result[key] = original

    def test_native_result_duplicate_comment_and_review_reference_rejected(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        result['provider_review_id'] = 45
        with patch.dict(c.os.environ,GITHUB_WORKFLOW_SHA='b'*40), self.assertRaises(Rejected):
            c.authenticated_execution(self.api,proof,self.policy,'claude',now)


    def test_native_opencode_review_reference_requires_exact_pr_head_and_app(self):
        proof, run, job, check, comment, result, now = self.native_fixture()
        self.policy['providers']['opencode']['identity'] = deepcopy(self.policy['providers']['claude']['identity'])
        result.update(provider='opencode',provider_review_id=44,policy_digest=digest(self.policy))
        result.pop('provider_comment_id')
        comment.update(pull_request_url=self.api.api_root+'/pulls/211',commit_id='a'*40,state='COMMENTED')
        with patch.dict(c.os.environ,GITHUB_WORKFLOW_SHA='b'*40):
            self.assertEqual(c.authenticated_execution(self.api,proof,self.policy,'opencode',now),result)
            comment['commit_id'] = 'f'*40
            with self.assertRaises(Rejected): c.authenticated_execution(self.api,proof,self.policy,'opencode',now)
            comment['commit_id'] = 'a'*40
            comment.pop('performed_via_github_app')
            with self.assertRaises(Rejected): c.authenticated_execution(self.api,proof,self.policy,'opencode',now)

    # --- Defect 1: transient GitHub API failures must not terminally BLOCK ---

    def test_transient_pull_fetch_preserves_task_and_recovers(self):
        original = self.api.request
        pulled = {'done': False}
        def flaky(path, method='GET', body=None):
            if path == '/pulls/211' and not pulled['done']:
                pulled['done'] = True
                raise TransientError('rate limited')
            return original(path, method, body)
        self.api.request = flaky
        try:
            with patch.object(c, 'choose', return_value='claude'):
                # First execution: a transient outage preserves the task (no BLOCK,
                # no journal write, no command issued).
                value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
                task = value['tasks'][self.task['id']]
                self.assertEqual(task['state'], 'ASSIGNED')
                self.assertEqual(self.store.writes, 0)
                self.assertEqual(self.api.posts, [])
                # The next scheduled execution retries and delivers.
                value, _ = c.reconcile(self.api, self.store, self.policy, value, blob, self.task['id'], 110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'INDEPENDENT_REVIEW')
                self.assertEqual(len(self.api.posts), 1)
        finally:
            self.api.request = original

    def test_definitive_pr_unavailable_still_blocks(self):
        original = self.api.request
        def flaky(path, method='GET', body=None):
            if path == '/pulls/211':
                raise Rejected('HTTP 404')
            return original(path, method, body)
        self.api.request = flaky
        try:
            value, _ = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
        finally:
            self.api.request = original
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['state'], 'BLOCKED')
        self.assertEqual(task['audit'][-1]['reason'], 'PR unavailable or no longer eligible')

    def test_transient_result_discovery_preserves_delivered_task(self):
        value, _ = self.run_task()  # delivery DELIVERED
        task_id = self.task['id']
        writes_before = self.store.writes
        original = c.execution_result
        c.execution_result = lambda *a, **k: (_ for _ in ()).throw(TransientError('outage'))
        try:
            value2, blob2 = c.reconcile(self.api, self.store, self.policy, value, '0', task_id, 110)
            self.assertEqual(value2, value)
            self.assertEqual(value2['tasks'][task_id]['state'], 'INDEPENDENT_REVIEW')
            self.assertEqual(self.store.writes, writes_before)
        finally:
            c.execution_result = original
        # Recovery: a genuine result completes the delivery.
        with patch.object(c, 'authenticated_execution',
                          return_value=dict(result='SUCCESS', binding=self.task['binding'], source_sha='b'*40,
                                            run_id=77, attempt=1, policy_digest=digest(self.policy),
                                            provider='claude', mode='review', provider_comment_id=44)), \
                patch.object(c, 'verify_check'):
            value3, _ = c.reconcile(self.api, self.store, self.policy, value2, blob2, task_id, 110)
        self.assertEqual(value3['tasks'][task_id]['deliveries'][-1]['status'], 'COMPLETED')
        self.assertEqual(value3['tasks'][task_id]['state'], 'ACCEPTED')

    def test_transient_post_after_reserve_keeps_reserved_without_repost(self):
        original = self.api.request
        def flaky(path, method='GET', body=None):
            if method == 'POST':
                raise TransientError('post outage')
            return original(path, method, body)
        self.api.request = flaky
        try:
            with patch.object(c, 'choose', return_value='claude'):
                value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
            # The RESERVED save was confirmed, so the committed pair is returned.
            task = value['tasks'][self.task['id']]
            self.assertEqual(task['state'], 'ASSIGNED')
            self.assertEqual(task['deliveries'][-1]['status'], 'RESERVED')
            self.assertEqual(blob, '1')
            # The RESERVED delivery was committed to the store before POST.
            committed = self.store.value['tasks'][self.task['id']]
            self.assertEqual(committed['deliveries'][-1]['status'], 'RESERVED')
            self.assertNotIn('comment_id', committed['deliveries'][-1])
            self.assertEqual(self.api.posts, [])
        finally:
            self.api.request = original

    def test_repeated_transient_interruptions_then_recovery(self):
        original = self.api.request
        outcomes = [TransientError('429'), TransientError('502'), None]
        calls = {'n': 0}
        def flaky(path, method='GET', body=None):
            if method == 'POST':
                return original(path, method, body)
            calls['n'] += 1
            item = outcomes[min(calls['n'] - 1, len(outcomes) - 1)]
            if isinstance(item, Exception):
                raise item
            return original(path, method, body)
        self.api.request = flaky
        try:
            with patch.object(c, 'choose', return_value='claude'):
                # Two executions hit transient outages; both preserve the task.
                value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'ASSIGNED')
                self.assertEqual(self.api.posts, [])
                value, blob = c.reconcile(self.api, self.store, self.policy, value, blob, self.task['id'], 110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'ASSIGNED')
                self.assertEqual(self.api.posts, [])
                # The third execution recovers after the repeated interruptions.
                value, _ = c.reconcile(self.api, self.store, self.policy, value, blob, self.task['id'], 110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'INDEPENDENT_REVIEW')
                self.assertEqual(len(self.api.posts), 1)
        finally:
            self.api.request = original

    def test_definitive_error_in_one_task_does_not_block_another(self):
        """A definitive (Rejected) failure in one task blocks only that task;
        independent tasks continue in the reconciliation loop."""
        second = deepcopy(self.task)
        second['id'] = 'pr-211-root-2'
        second['root'] = 2
        second['binding'] = dict(self.task['binding'])
        # Add an owner root comment for the second task so it can reconcile.
        root2 = deepcopy(self.api.root_comment)
        root2['id'] = 2
        self.api.comments.append(root2)
        second['root_digest'] = digest(comment_binding(root2))
        state_two = dict(version=1, revision=0, tasks={
            self.task['id']: self.task,
            second['id']: second,
        })
        original = self.api.request
        calls = {'n': 0}
        def flaky(path, method='GET', body=None):
            calls['n'] += 1
            if path == '/pulls/211' and calls['n'] == 1:
                raise Rejected('HTTP 404')
            return original(path, method, body)
        self.api.request = flaky
        try:
            with patch.object(c, 'choose', return_value='claude'):
                value, blob = c.reconcile(self.api, self.store, self.policy, state_two, '0', self.task['id'], 110)
                self.assertEqual(value['tasks'][self.task['id']]['state'], 'BLOCKED')
                # The second task is unaffected and proceeds normally.
                value2, blob2 = c.reconcile(self.api, self.store, self.policy, value, blob, second['id'], 110)
                self.assertEqual(value2['tasks'][second['id']]['state'], 'INDEPENDENT_REVIEW')
                self.assertEqual(len(self.api.posts), 1)
        finally:
            self.api.request = original

    # --- Defect 2: provider-run discovery beyond the newest 20 runs ---

    def delivered_task(self):
        task = deepcopy(self.task)
        task['deliveries'] = [dict(id='nonce-1', provider='claude', status='DELIVERED',
                                   comment_id=20, created_at=100)]
        self.policy['providers']['claude']['proof'] = dict(run_id=77, workflow_id=88,
            workflow_path='.github/workflows/claude.yml', jobs=['review'])
        return task

    def _run_result(self, run_id):
        return dict(result='SUCCESS', binding=self.task['binding'], source_sha='a'*40,
                    run_id=run_id, attempt=1, policy_digest=digest(self.policy),
                    provider='claude', mode='review', provider_comment_id=5)

    def test_execution_result_finds_genuine_beyond_first_twenty(self):
        # 25 newer unrelated runs would displace the genuine (oldest) run under
        # the old :20 slice; bounded pagination must still find it.
        genuine = dict(id=1, status='completed', conclusion='success', workflow_id=88,
                       head_branch='main', event='issue_comment', head_sha='a'*40)
        unrelated = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                          head_branch='main', event='issue_comment', head_sha='z'*40)
                     for i in range(2, 27)]
        self.api.pages = lambda path, key=None: deepcopy([genuine] + unrelated)
        def fake_auth(api, proof, policy, provider, now, task=None, delivery=None):
            if proof['run_id'] == 1:
                return self._run_result(1)
            raise Rejected('not our run')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            outcome, run_id, _ = c.execution_result(self.api, self.policy, self.delivered_task(), 110)
        self.assertEqual(run_id, 1)
        self.assertEqual(outcome, 'SUCCESS')

    def test_execution_result_missing_result(self):
        self.api.pages = lambda path, key=None: deepcopy([dict(id=5, status='completed',
            conclusion='success', workflow_id=88, head_branch='main', event='issue_comment', head_sha='z'*40)])
        with patch.object(c, 'authenticated_execution', side_effect=Rejected('not our run')), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, self.delivered_task(), 110))

    def test_execution_result_duplicate_ambiguous_rejected(self):
        # Contradictory results (different digests) → Rejected.
        runs = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', head_sha='a'*40) for i in (1, 2)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        results = [self._run_result(1), self._run_result(2)]
        # Make results contradictory by changing result field.
        results[1] = dict(results[1], result='FAILED')
        call_count = {'n': 0}
        def fake_auth(api, proof, policy, provider, now, task=None, delivery=None):
            r = results[call_count['n']]
            call_count['n'] += 1
            return r
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            with self.assertRaises(Rejected):
                c.execution_result(self.api, self.policy, self.delivered_task(), 110)

    def test_execution_result_spoofed_or_stale_run_rejected(self):
        runs = [dict(id=1, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', head_sha='a'*40)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        with patch.object(c, 'authenticated_execution', side_effect=Rejected('spoofed')), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, self.delivered_task(), 110))

    def test_execution_result_stale_revision_skipped(self):
        runs = [dict(id=1, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', head_sha='f'*40)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        # The run's head_sha (f*40) is a main SHA, not PR head. But since we
        # no longer filter on head_sha, authenticated_execution must reject it.
        with patch.object(c, 'authenticated_execution', side_effect=Rejected('not our run')), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, self.delivered_task(), 110))

    def test_execution_result_skips_incomplete_and_skipped_runs(self):
        runs = [
            dict(id=1, status='in_progress', conclusion=None, workflow_id=88, head_branch='main',
                 event='issue_comment', head_sha='a'*40),
            dict(id=2, status='completed', conclusion='skipped', workflow_id=88, head_branch='main',
                 event='issue_comment', head_sha='a'*40),
            dict(id=3, status='completed', conclusion='success', workflow_id=88, head_branch='main',
                 event='issue_comment', head_sha='a'*40),
        ]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        calls = []
        def fake_auth(api, proof, policy, provider, now, task=None, delivery=None):
            calls.append(proof['run_id'])
            return self._run_result(proof['run_id'])
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            task = self.delivered_task()
            # Skipped run 2 is ignored, but unfinished run 1 may still become the
            # result, so run 3 is authenticated yet acceptance is withheld.
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            self.assertEqual(calls, [3])
            runs[0].update(status='completed', conclusion='skipped')
            outcome, run_id, _ = c.execution_result(self.api, self.policy, task, 110)
        self.assertEqual(run_id, 3)
        self.assertEqual(calls, [3, 3])  # scanned once; retained match re-authenticated live

    def test_unfinished_run_cannot_hide_contradictory_evidence(self):
        runs = [dict(id=1, status='in_progress', conclusion=None, workflow_id=88, head_branch='main',
                     event='issue_comment'),
                dict(id=3, status='completed', conclusion='success', workflow_id=88, head_branch='main',
                     event='issue_comment')]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        def fake_auth(api, proof, *a, **k):
            result = self._run_result(proof['run_id'])
            return dict(result, provider_comment_id=6) if proof['run_id'] == 1 else result
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            runs[0].update(status='completed', conclusion='success')
            with self.assertRaises(Rejected):
                c.execution_result(self.api, self.policy, task, 110)

    def test_scan_interval_never_straddles_unfinished_run(self):
        runs = [dict(id=i, status='in_progress' if i == 2 else 'completed',
                     conclusion=None if i == 2 else 'success', workflow_id=88,
                     head_branch='main', event='issue_comment') for i in (1, 2, 3)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        seen = []
        def fake_auth(api, proof, *a, **k):
            seen.append(proof['run_id'])
            raise Rejected('other')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            c.execution_result(self.api, self.policy, task, 110)
            self.assertEqual(seen, [3])  # 1 would straddle pending run 2
            runs[1].update(status='completed', conclusion='skipped')
            c.execution_result(self.api, self.policy, task, 110)
        self.assertEqual(seen, [3, 1])

    def test_execution_result_handles_over_200_unrelated_runs(self):
        unrelated = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                          head_branch='main', event='issue_comment', head_sha='z'*40)
                     for i in range(1, 202)]
        genuine = dict(id=202, status='completed', conclusion='success', workflow_id=88,
                       head_branch='main', event='issue_comment', head_sha='a'*40)
        self.api.pages = lambda path, key=None: deepcopy(unrelated + [genuine])
        def fake_auth(api, proof, policy, provider, now, task=None, delivery=None):
            if proof['run_id'] == 202:
                return self._run_result(202)
            raise Rejected('not our run')
        task = self.delivered_task()
        calls = []
        def counting(api, proof, *a, **k):
            calls.append(proof['run_id'])
            return fake_auth(api, proof, *a, **k)
        with patch.object(c, 'authenticated_execution', side_effect=counting), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='a'*40):
            # Passes 1-2 are incomplete (budget 100/pass) but persist progress and
            # the retained match; pass 3 completes and returns the genuine run.
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            self.assertEqual(len(calls), 100)
            self.assertEqual(task['deliveries'][-1]['scan']['match']['run_id'], 202)
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            self.assertEqual(len(calls), 200)
            self.assertEqual(len(set(calls)), 200)  # never re-authenticates examined runs
            outcome, run_id, _ = c.execution_result(self.api, self.policy, task, 110)
        self.assertEqual((outcome, run_id), ('SUCCESS', 202))

    def test_scan_progress_survives_100_plus_candidates_and_new_runs(self):
        runs = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', head_sha='m'*40)
                for i in range(1, 151)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        def fake_auth(api, proof, *a, **k):
            if proof['run_id'] == 1:
                return self._run_result(1)
            raise Rejected('other task')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            runs.append(dict(runs[0], id=151))  # a new unrelated run arrives
            outcome, run_id, _ = c.execution_result(self.api, self.policy, task, 110)
        self.assertEqual(run_id, 1)

    def test_contradiction_on_later_pass_fails_closed(self):
        runs = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', head_sha='m'*40)
                for i in range(1, 151)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        def fake_auth(api, proof, *a, **k):
            if proof['run_id'] == 150:
                return self._run_result(150)
            if proof['run_id'] == 1:
                return dict(self._run_result(1), provider_comment_id=6)
            raise Rejected('other task')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            with self.assertRaises(Rejected):
                c.execution_result(self.api, self.policy, task, 110)

    def test_pending_run_blocks_acceptance_until_it_completes(self):
        runs = [dict(id=1, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment'),
                dict(id=2, status='in_progress', conclusion=None, workflow_id=88,
                     head_branch='main', event='issue_comment')]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        with patch.object(c, 'authenticated_execution', return_value=self._run_result(1)), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            runs[1].update(status='completed', conclusion='skipped')
            self.assertEqual(c.execution_result(self.api, self.policy, task, 110)[1], 1)

    def _scan_runs(self, n=150):
        return [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment', run_attempt=1) for i in range(1, n + 1)]

    def test_rerun_inside_scanned_interval_is_reexamined(self):
        runs = self._scan_runs()
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        def fake_auth(api, proof, *a, **k):
            if proof['run_id'] == 100:
                return self._run_result(100)
            if proof['run_id'] == 75 and runs[74]['run_attempt'] == 2:
                return dict(self._run_result(75), result='FAILED')
            raise Rejected('other')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
            self.assertEqual(task['deliveries'][-1]['scan']['match']['run_id'], 100)
            runs[74].update(run_attempt=2)  # rerun of an already-examined run
            with self.assertRaises(Rejected):
                c.execution_result(self.api, self.policy, task, 110)

    def test_unchanged_runs_are_not_rescanned(self):
        runs = self._scan_runs()
        self.api.pages = lambda path, key=None: deepcopy(runs)
        task = self.delivered_task()
        calls = []
        def fake_auth(api, proof, *a, **k):
            calls.append(proof['run_id'])
            raise Rejected('other')
        with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            c.execution_result(self.api, self.policy, task, 110)
            c.execution_result(self.api, self.policy, task, 110)
        self.assertEqual(sorted(calls), list(range(1, 151)))

    def test_retained_match_is_reauthenticated_before_acceptance(self):
        for mode in ('rejected', 'changed', 'unchanged'):
            runs = self._scan_runs()
            self.api.pages = lambda path, key=None: deepcopy(runs)
            task = self.delivered_task()
            state = {'phase': 1}
            def fake_auth(api, proof, *a, **k):
                if proof['run_id'] != 100:
                    raise Rejected('other')
                if state['phase'] == 2 and mode == 'rejected':
                    raise Rejected('witness edited/deleted')
                if state['phase'] == 2 and mode == 'changed':
                    return dict(self._run_result(100), provider_comment_id=9)
                return self._run_result(100)
            with patch.object(c, 'authenticated_execution', side_effect=fake_auth), \
                    patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
                self.assertIsNone(c.execution_result(self.api, self.policy, task, 110))
                state['phase'] = 2
                if mode == 'unchanged':
                    self.assertEqual(c.execution_result(self.api, self.policy, task, 110)[1], 100)
                else:
                    with self.assertRaises(Rejected, msg=mode):
                        c.execution_result(self.api, self.policy, task, 110)

    def test_duplicate_runs_same_reference_resolve_deterministically(self):
        runs = [dict(id=i, status='completed', conclusion='success', workflow_id=88,
                     head_branch='main', event='issue_comment') for i in (4, 9)]
        self.api.pages = lambda path, key=None: deepcopy(runs)
        with patch.object(c, 'authenticated_execution', side_effect=lambda api, proof, *a, **k: self._run_result(proof['run_id'])), \
                patch.dict(c.os.environ, GITHUB_WORKFLOW_SHA='m'*40):
            self.assertEqual(c.execution_result(self.api, self.policy, self.delivered_task(), 110)[1], 4)

    # --- Journal authority: confirmed saves are never rolled back ---

    def test_transient_after_reserved_save_returns_committed_pair_and_next_task_cas_ok(self):
        original = self.api.request
        def flaky(path, method='GET', body=None):
            if method == 'POST':
                raise TransientError('5xx')
            return original(path, method, body)
        self.api.request = flaky
        with patch.object(c, 'choose', return_value='claude'):
            value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
        self.assertEqual(blob, str(self.store.writes))  # current CAS blob, not '0'
        self.assertEqual(value['revision'], self.store.value['revision'])
        self.assertEqual(value['tasks'][self.task['id']]['deliveries'][-1]['status'], 'RESERVED')
        self.api.request = original
        # A following independent save uses the current blob and revision.
        self.store.save(value, blob)

    def test_rejected_after_reserved_save_escalates_from_committed_state(self):
        original = self.api.request
        calls = {'n': 0}
        def flaky(path, method='GET', body=None):
            if path == '/pulls/211':
                calls['n'] += 1
                if calls['n'] >= 2:  # second read, after RESERVED was saved
                    return dict(original(path), head={'sha': 'c'*40, 'repo': {'full_name': self.policy['repository']}})
            return original(path, method, body)
        self.api.request = flaky
        seen = []
        real_save = self.store.save
        def save(value, sha):
            seen.append(sha)
            return real_save(value, sha)
        self.store.save = save
        with patch.object(c, 'choose', return_value='claude'):
            value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
        task = value['tasks'][self.task['id']]
        self.assertEqual(task['state'], 'ESCALATED')
        self.assertEqual(task['deliveries'][-1]['status'], 'RESERVED')  # not rolled back
        self.assertEqual(seen[-1], '1')  # escalation CAS uses the committed blob
        self.assertEqual(self.api.posts, [])

    def test_nothing_saved_returns_exact_original(self):
        original = self.api.request
        self.api.request = lambda path, method='GET', body=None: (_ for _ in ()).throw(TransientError('x'))
        value, blob = c.reconcile(self.api, self.store, self.policy, self.state, '0', self.task['id'], 110)
        self.api.request = original
        self.assertIs(value, self.state)
        self.assertEqual(blob, '0')


class StoreAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT/c.POLICY).read_text())
        self.policy['issuer'] = dict(id=100, login='native[bot]', app_id=200, app_slug='native')
        self.value = dict(version=1, revision=3, tasks={})

    def store(self, put, head):
        store = c.Store.__new__(c.Store)
        store.policy = self.policy
        class Api:
            def request(inner, path, method='GET', body=None):
                if method == 'PUT':
                    return put()
                raise AssertionError(path)
        store.api = Api()
        store.read = head
        return store

    def test_put_timeout_that_landed_is_recognized(self):
        landed = dict(self.value, revision=4)
        def put(): raise TransientError('timeout')
        store = self.store(put, lambda: (deepcopy(landed), 'new'))
        value, blob = store.save(self.value, 'old')
        self.assertEqual((value, blob), (landed, 'new'))

    def test_put_timeout_not_applied_is_transient_with_unchanged_head(self):
        def put(): raise TransientError('timeout')
        store = self.store(put, lambda: (deepcopy(self.value), 'old'))
        with self.assertRaises(TransientError):
            store.save(self.value, 'old')

    def test_put_timeout_and_unreadable_head_fails_closed(self):
        def put(): raise TransientError('timeout')
        def head(): raise TransientError('down')
        with self.assertRaises(c.AmbiguousWrite):
            self.store(put, head).save(self.value, 'old')

    def test_put_timeout_head_moved_elsewhere_fails_closed(self):
        def put(): raise TransientError('timeout')
        store = self.store(put, lambda: (dict(self.value, revision=9), 'other'))
        with self.assertRaises(c.AmbiguousWrite):
            store.save(self.value, 'old')


class TransportTests(unittest.TestCase):
    """Transport-level tests of GitHub.request retry/backoff behavior."""

    def setUp(self):
        self.policy = json.loads((ROOT / c.POLICY).read_text())
        self.policy['issuer'] = dict(id=100, login='native[bot]', app_id=200, app_slug='native')
        os.environ['GITHUB_TOKEN'] = 'test-token-123'

    def _make_api(self):
        return c.GitHub(self.policy, root=ROOT)

    def test_transport_timeout_exhaustion_raises_transient_error(self):
        """A persistent transport TimeoutError raises TransientError after bounded retries."""
        api = self._make_api()
        attempts = {'n': 0}
        def fail_open(request, timeout=25):
            attempts['n'] += 1
            raise TimeoutError('gateway timeout')
        api.opener.open = fail_open
        with patch.object(api, '_retry_delay', return_value=0):
            with self.assertRaises(TransientError):
                api.request('/pulls/1')
        self.assertEqual(attempts['n'], MAX_REQUEST_ATTEMPTS)

    def test_transport_429_exhaustion_raises_transient_error_not_rejected(self):
        """A persistent HTTP 429 raises TransientError after bounded retries, never Rejected."""
        import urllib.error
        api = self._make_api()
        attempts = {'n': 0}
        def fail_open(request, timeout=25):
            attempts['n'] += 1
            raise urllib.error.HTTPError(
                'https://api.github.com/repos/o/r/pulls/1', 429,
                'Too Many Requests', {}, None)
        api.opener.open = fail_open
        with patch.object(api, '_retry_delay', return_value=0):
            with self.assertRaises(TransientError):
                api.request('/pulls/1')
        self.assertEqual(attempts['n'], 5)

    def test_transport_definitive_404_raises_rejected_immediately_no_retry(self):
        """A definitive HTTP 404 raises Rejected immediately, with no retry."""
        import urllib.error
        api = self._make_api()
        attempts = {'n': 0}
        def fail_open(request, timeout=25):
            attempts['n'] += 1
            raise urllib.error.HTTPError(
                'https://api.github.com/repos/o/r/pulls/1', 404,
                'Not Found', {}, None)
        api.opener.open = fail_open
        with self.assertRaises(Rejected):
            api.request('/pulls/1')
        self.assertEqual(attempts['n'], 1)

    def test_transport_503_exhaustion_raises_transient_error(self):
        """A persistent HTTP 503 raises TransientError after bounded retries."""
        import urllib.error
        api = self._make_api()
        attempts = {'n': 0}
        def fail_open(request, timeout=25):
            attempts['n'] += 1
            raise urllib.error.HTTPError(
                'https://api.github.com/repos/o/r/pulls/1', 503,
                'Service Unavailable', {}, None)
        api.opener.open = fail_open
        with patch.object(api, '_retry_delay', return_value=0):
            with self.assertRaises(TransientError):
                api.request('/pulls/1')
        self.assertEqual(attempts['n'], 5)

    def test_transport_urlerror_exhaustion_raises_transient_error(self):
        """A persistent URLError raises TransientError after bounded retries."""
        import urllib.error
        api = self._make_api()
        attempts = {'n': 0}
        def fail_open(request, timeout=25):
            attempts['n'] += 1
            raise urllib.error.URLError('network down')
        api.opener.open = fail_open
        with patch.object(api, '_retry_delay', return_value=0):
            with self.assertRaises(TransientError):
                api.request('/pulls/1')
        self.assertEqual(attempts['n'], 5)


if __name__ == '__main__': unittest.main()
