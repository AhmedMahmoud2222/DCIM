"""Security and recovery tests of the actual collaboration reconciler."""
import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import orchestration_controller as c
from acceptance_core import Rejected, digest
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
            item = dict(id=20, body=body['body'], user={k:v for k,v in self.policy['issuer'].items() if k in ('id', 'login')},
                        performed_via_github_app={'id': self.policy['issuer']['app_id'], 'slug': self.policy['issuer']['app_slug']}, created_at='now', updated_at='now')
            item['user']['type'] = 'Bot'
            self.comments.append(item)
            return item
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path.startswith('/issues/'):
            return deepcopy(self.comments)
        if path.startswith('/actions/workflows/'):
            return [{'id': 77, 'status': 'completed'}]
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
        self.policy['providers']['claude']['proof'] = dict(run_id=77, workflow_id=88, workflow_path='wf', jobs=['review'])
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
        with self.assertRaises(Rejected): self.run_task()
        self.api.raise_post = False
        value, _ = self.run_task(self.store.value)
        self.assertEqual(value['tasks'][self.task['id']]['state'], 'ESCALATED')
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
        with self.assertRaises(Rejected): self.run_task(reserved)
        self.assertEqual(len(self.api.posts), 1)

    def test_spoofed_app_cannot_recover(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        self.api.comments[-1]['performed_via_github_app']['id'] = 999
        with self.assertRaises(Rejected): self.run_task(reserved)

    def test_edited_delivery_cannot_recover(self):
        self.run_task()
        reserved = deepcopy(self.store.value)
        reserved['tasks'][self.task['id']]['deliveries'][0]['status'] = 'RESERVED'
        self.api.comments[-1]['updated_at'] = 'later'
        with self.assertRaises(Rejected): self.run_task(reserved)

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
        run = dict(id=77, workflow_id=88, path='wf', head_sha='b'*40, head_branch='main',
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
        self.api.request = lambda path, *args, **kwargs: deepcopy(run if path.startswith('/actions/runs/') else comment)
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


if __name__ == '__main__': unittest.main()
