"""Native Claude adapter authentication, isolation and sealing regressions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'.github/scripts'))
import orchestration_claude_adapter as a
from acceptance_core import Rejected,digest


class API:
    def __init__(self):
        self.root=ROOT
        self.policy=json.loads((ROOT/a.POLICY).read_text())
        self.policy['enabled']=True
        self.policy['providers']['claude']['enabled']=True
        self.policy['issuer']=dict(id=100,login='source[bot]',app_id=200,app_slug='source')
        self.api_root='https://api.github.com/repos/'+self.policy['repository']
        self.b=dict(repository=self.policy['repository'],pr=211,head='a'*40,base='b'*40)
        self.pr=dict(number=211,state='open',user={'id':285010264},changed_files=1,
                     head={'sha':'a'*40,'repo':{'full_name':self.policy['repository']}},
                     base={'sha':'b'*40,'ref':'main','repo':{'full_name':self.policy['repository']}})
        owner=dict(self.policy['owner'])
        self.root_comment=dict(id=1,body='/oc review independently\nDCIM-Head: '+'a'*40,user=owner,
                               author_association='OWNER',created_at='2026-10-28T01:00:00Z',updated_at='2026-10-28T01:00:00Z')
        self.comment=dict(id=2,body='/dcim-claude review independently\nDCIM-Task: pr-211-root-1\nDCIM-Delivery: '+'c'*32+'\nDCIM-Root: 1\nDCIM-Head: '+'a'*40,
                          user={'id':100,'login':'source[bot]','type':'Bot'},performed_via_github_app={'id':200,'slug':'source'},
                          created_at='2026-10-28T01:01:00Z',updated_at='2026-10-28T01:01:00Z',issue_url=self.api_root+'/issues/211')
        self.comments=[self.root_comment,self.comment]
        self.event=dict(action='created',issue={'number':211,'pull_request':{}},comment=deepcopy(self.comment),sender=deepcopy(self.comment['user']))
        self.run=dict(id=77,run_attempt=1,check_suite_id=9,head_sha='b'*40,head_branch='main',workflow_id=88,path=a.WORKFLOW,
                      event='issue_comment',repository={'full_name':self.policy['repository']},head_repository={'full_name':self.policy['repository']},
                      actor=deepcopy(self.comment['user']),triggering_actor=deepcopy(self.comment['user']))
        self.posts=[]
        self.witness_comment=None
        self.jobs=[dict(id=10+i,name=name,run_id=77,run_attempt=1,head_sha='b'*40,status='completed',conclusion='success',
                        check_run_url=self.api_root+'/check-runs/'+str(20+i),started_at='2026-10-28T01:01:00Z',completed_at='2026-10-28T01:03:00Z') for i,name in enumerate(('prepare','review'))]
        self.checks=[dict(id=20+i,name=name,head_sha='b'*40,status='completed',conclusion='success',app={'id':15368,'slug':'github-actions'},check_suite={'id':9}) for i,name in enumerate(('prepare','review'))]
        self.trusted=[]

    def request(self,path,method='GET',body=None):
        if method=='POST':
            self.posts.append(body['body'])
            self.witness_comment=dict(id=44,body=body['body'],user={'id':a.CLAUDE['id'],'login':a.CLAUDE['login'],'type':'Bot'},
                                      performed_via_github_app={'id':a.CLAUDE['app_id'],'slug':a.CLAUDE['app_slug']},
                                      issue_url=self.api_root+'/issues/211',created_at='2026-10-28T01:02:00Z',updated_at='2026-10-28T01:02:00Z')
            return deepcopy(self.witness_comment)
        if path=='/pulls/211':return deepcopy(self.pr)
        if path=='/issues/comments/'+str(self.comment['id']):return deepcopy(self.comment)
        if path=='/issues/comments/44':return deepcopy(self.witness_comment)
        if path=='/actions/runs/77':return deepcopy(self.run)
        if path=='/branches/main':return {'commit':{'sha':'b'*40}}
        if path=='/commits/'+'b'*40:return {'sha':'b'*40,'commit':{'verification':{'verified':True,'reason':'valid'}}}
        raise AssertionError(path)

    def pages(self,path,key=None):
        if path=='/issues/211/comments':return deepcopy(self.comments)
        if path=='/pulls/211/files':return [dict(filename='docs/example.md',patch='@@ -1 +1 @@\n-old\n+new',additions=1,deletions=1)]
        if key=='jobs':return deepcopy(self.jobs)
        if key=='check_runs':return deepcopy(self.checks)
        raise AssertionError(path)

    def file(self,path,sha):return json.dumps(self.policy).encode()
    def workflow(self,path):return 88
    def trusted_file(self,path,sha):self.trusted.append((path,sha))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.api=API()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.event_path=Path(self.temp.name)/'event.json'
        self.event_path.write_text(json.dumps(self.api.event))
        self.output=Path(self.temp.name)/'outputs'
        self.env=patch.dict(os.environ,GITHUB_REPOSITORY=self.api.policy['repository'],GITHUB_EVENT_NAME='issue_comment',
                            GITHUB_REF='refs/heads/main',GITHUB_WORKFLOW_REF=self.api.policy['repository']+'/'+a.WORKFLOW+'@refs/heads/main',
                            GITHUB_WORKFLOW_SHA='b'*40,GITHUB_RUN_ID='77',GITHUB_RUN_ATTEMPT='1',GITHUB_EVENT_PATH=str(self.event_path),
                            RUNNER_TEMP=self.temp.name,GITHUB_OUTPUT=str(self.output),GITHUB_TOKEN='test-only-not-secret')
        self.env.start()
        self.addCleanup(self.env.stop)

    def request_value(self):return a.authenticate(self.api,self.api.event)

    def test_real_root_authorization_adaptation_preserves_original_event(self):
        before=deepcopy(self.api.event)
        value=self.request_value()
        self.assertEqual(value['task_id'],'pr-211-root-1')
        self.assertEqual(value['delivery_id'],'c'*32)
        self.assertEqual(self.api.event,before)

    def test_wrong_app_actor_or_sender_rejected(self):
        for target,key,bad in ((self.api.event['comment']['performed_via_github_app'],'id',999),
                               (self.api.event['comment']['user'],'id',999),(self.api.event['sender'],'id',999)):
            old=target[key];target[key]=bad
            with self.assertRaises(Rejected):self.request_value()
            target[key]=old

    def test_edited_api_command_rejected(self):
        self.api.comment['updated_at']='later'
        with self.assertRaises(Rejected):self.request_value()

    def test_fresh_but_edited_event_and_api_rejected(self):
        self.api.comment['updated_at']='later'
        self.api.event['comment']['updated_at']='later'
        with self.assertRaises(Rejected):self.request_value()

    def test_wrong_head_or_base_rejected(self):
        for side in ('head','base'):
            original=self.api.pr[side]['sha'];self.api.pr[side]['sha']='f'*40
            with self.assertRaises(Rejected):self.request_value()
            self.api.pr[side]['sha']=original

    def test_self_review_rejected(self):
        self.api.policy['implementers']['211']='claude'
        with self.assertRaises(Rejected):self.request_value()

    def test_pr_author_cannot_review_own_work(self):
        self.api.pr['user']['id']=a.CLAUDE['id']
        with self.assertRaises(Rejected):self.request_value()

    def test_missing_owner_root_or_duplicate_history_rejected(self):
        self.api.comments=[self.api.comment]
        with self.assertRaises(Rejected):self.request_value()
        self.api.comments=[self.api.root_comment,self.api.comment,self.api.comment]
        with self.assertRaises(Rejected):self.request_value()

    def test_non_review_or_mismatched_task_root_rejected(self):
        for old,new in (('/dcim-claude review','/dcim-claude implement'),('pr-211-root-1','pr-212-root-1'),('pr-211-root-1','pr-211-root-9')):
            current=deepcopy(self.api.comment)
            self.api.comment['body']=self.api.comment['body'].replace(old,new)
            self.api.event['comment']=deepcopy(self.api.comment)
            with self.assertRaises(Rejected):self.request_value()
            self.api.comment.clear();self.api.comment.update(current)

    def test_budget_counts_other_native_comments_and_refuses_fifth(self):
        self.api.comment['id']=10;self.api.event['comment']['id']=10
        self.api.comments=[self.api.root_comment]+[dict(deepcopy(self.api.comment),id=i) for i in range(2,6)]+[self.api.comment]
        with self.assertRaises(Rejected):self.request_value()

    def test_atomic_claim_failure_blocks_prepare_before_model_context(self):
        with patch.object(a.agent_authority,'claim',return_value=False),patch.object(a,'prepare_context') as context:
            with self.assertRaises(Rejected):a.prepare(self.api)
            context.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_prepare_outputs_exact_allowed_bot_and_read_only_args(self):
        with patch.object(a.agent_authority,'claim',return_value=True) as claim,patch.object(a,'prepare_context'):
            a.prepare(self.api)
            claim.assert_called_once()
        text=self.output.read_text()
        self.assertIn('allowed_bot=source[bot]',text)
        self.assertIn('--tools Read',text)
        self.assertNotIn('allowed_bot=*',text)
        self.assertIn('--permission-mode dontAsk',text)

    def test_runtime_requires_enabled_default_branch_and_source_actor(self):
        run,sha=a.runtime(self.api)
        self.assertEqual(sha,'b'*40)
        self.assertEqual(run['id'],77)
        with patch.dict(os.environ,GITHUB_REF='refs/heads/feature'):
            with self.assertRaises(Rejected):a.runtime(self.api)
        self.api.run['triggering_actor']['id']=999
        with self.assertRaises(Rejected):a.runtime(self.api)
        self.api.run['triggering_actor']['id']=100
        self.api.policy['enabled']=False
        with self.assertRaises(Rejected):a.runtime(self.api)

    def test_native_hook_clears_credentials_before_runtime_network(self):
        with patch.object(a,'clear_git_credentials') as clear,patch.object(a,'runtime',side_effect=Rejected('no network')):
            with self.assertRaises(Rejected):a.witness(self.api)
            clear.assert_called_once_with(self.api.root,self.api.policy['repository'])

    def test_native_witness_is_actual_app_and_idempotent(self):
        request=self.request_value()
        with patch.dict(os.environ,REVIEW_REQUEST=json.dumps(request)),patch.object(a,'clear_git_credentials'):
            a.witness(self.api)
            a.witness(self.api)
        self.assertEqual(len(self.api.posts),1)
        self.assertEqual(json.loads(a.witness_path().read_text()),{'id':44})

    def test_missing_or_forged_native_witness_is_not_retried(self):
        request=self.request_value()
        with patch.dict(os.environ,REVIEW_REQUEST=json.dumps(request)),patch.object(a,'clear_git_credentials'):
            a.witness_path().write_text('{}')
            with self.assertRaises(KeyError):a.witness(self.api)
        self.assertEqual(self.api.posts,[])

    def seal_setup(self):
        request=self.request_value()
        review=dict(binding=request['binding'],reviewer='claude',verdict='BLOCK',findings=[dict(id='finding-1',blocking=True,message='Requires remediation')],resolved_finding_ids=[])
        with patch.dict(os.environ,REVIEW_REQUEST=json.dumps(request)),patch.object(a,'clear_git_credentials'):a.witness(self.api)
        env=dict(REVIEW_REQUEST=json.dumps(request),NATIVE_CONCLUSION='success',REVIEW_JSON=json.dumps(review),NATIVE_SESSION='session_1',PROVIDER_COMMENT_ID='44')
        return request,review,env

    def test_seal_block_verdict_is_execution_evidence_not_acceptance(self):
        request,review,env=self.seal_setup()
        with patch.dict(os.environ,**env),patch.object(a,'write_record') as write:
            a.seal(self.api)
            value=write.call_args.args[1]
        self.assertEqual(value['result'],'SUCCESS')
        self.assertEqual(value['verdict'],'BLOCK')
        self.assertEqual(value['policy_digest'],digest(self.api.policy))
        self.assertEqual(value['provider_comment_id'],44)
        self.assertEqual(value['delivery_id'],'c'*32)
        self.assertEqual(value['source_sha'],'b'*40)
        self.assertEqual(value['run_id'],77)
        self.assertEqual(value['attempt'],1)
        self.assertEqual(value['source'],dict(run_id=77,attempt=1,trust_sha='b'*40))
        self.assertTrue(any('not merge authorization' in text for text in self.api.posts))

    def test_seal_rejects_forged_app_check_and_stale_witness(self):
        request,review,env=self.seal_setup()
        self.api.checks[0]['app']['id']=999
        with patch.dict(os.environ,**env),self.assertRaises(Rejected):a.seal(self.api)
        self.api.checks[0]['app']['id']=15368
        self.api.witness_comment['body']='ordinary Claude prose ACCEPT'
        with patch.dict(os.environ,**env),self.assertRaises(Rejected):a.seal(self.api)

    def test_seal_rejects_accept_with_unresolved_findings(self):
        request,review,env=self.seal_setup()
        review['verdict']='ACCEPT';env['REVIEW_JSON']=json.dumps(review)
        with patch.dict(os.environ,**env),self.assertRaises(Rejected):a.seal(self.api)

    def test_seal_rejects_failed_native_action_and_missing_witness(self):
        request,review,env=self.seal_setup()
        env['NATIVE_CONCLUSION']='failure'
        with patch.dict(os.environ,**env),self.assertRaises(Rejected):a.seal(self.api)
        env['NATIVE_CONCLUSION']='success';env['PROVIDER_COMMENT_ID']='0'
        with patch.dict(os.environ,**env),self.assertRaises(Rejected):a.seal(self.api)

    def test_workflow_native_action_and_tool_boundary_pins(self):
        text=(ROOT/a.WORKFLOW).read_text()
        self.assertIn('allowed_bots: ${{ needs.prepare.outputs.allowed_bot }}',text)
        self.assertNotIn('allowed_bots: "*"',text)
        self.assertIn('uses: '+a.ACTION,text)
        self.assertIn('orchestration_claude_adapter.py witness',text)
        self.assertIn('acceptance_read_guard.py',text)
        self.assertNotIn('pull_request_target:',text)
        self.assertNotIn('contents: write',text)
        self.assertIn('dcim-agent-result-attempt-${{ github.run_attempt }}',text)


    def test_captured_binding_survives_base_advance_but_changed_head_is_rejected(self):
        request=self.request_value()
        self.api.pr['base']['sha']='f'*40
        self.assertEqual(a.authenticate(self.api,self.api.event,captured=request),request)
        with self.assertRaises(Rejected):self.request_value()
        self.api.pr['head']['sha']='e'*40
        with self.assertRaises(Rejected):a.authenticate(self.api,self.api.event,captured=request)

    def test_runtime_historical_policy_is_independently_authenticated(self):
        with patch.object(a,'execution_policy',return_value=deepcopy(self.api.policy)) as continuity:
            a.runtime(self.api)
            continuity.assert_called_once_with(self.api,self.api.policy,'b'*40,'b'*40)
        with patch.object(a,'execution_policy',return_value=dict(self.api.policy,enabled=False)):
            with self.assertRaises(Rejected):a.runtime(self.api)

    def test_seal_preserves_original_base_after_main_advance(self):
        request,review,env=self.seal_setup()
        self.api.pr['base']['sha']='f'*40
        with patch.dict(os.environ,**env),patch.object(a,'write_record') as write:
            a.seal(self.api)
        self.assertEqual(write.call_args.args[1]['binding']['base'],'b'*40)
        self.assertEqual(write.call_args.args[1]['source']['trust_sha'],'b'*40)


if __name__=='__main__':unittest.main()
