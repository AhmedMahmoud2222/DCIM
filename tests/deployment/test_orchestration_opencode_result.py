"""Test the actual native execution adapter; no review text grants acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.github/scripts'))
import orchestration_opencode_result as adapter
from acceptance_core import Rejected

class NativeResultTests(unittest.TestCase):
    def test_real_workflow_supplies_policy_and_source_read_permission(self):
        root = Path(__file__).resolve().parents[2]
        steps = yaml.safe_load((root/'.github/workflows/opencode.yml').read_text())['jobs']['review_publish']['steps']
        checkout = next(s for s in steps if 'checkout@' in s.get('uses', ''))
        self.assertIn('.github/agents', checkout['with']['sparse-checkout'].split())
        mint = next(s for s in steps if 'create-github-app-token@' in s.get('uses', ''))
        self.assertEqual(mint['with']['permission-contents'], 'read')
        seal = next(s for s in steps if 'orchestration_opencode_result.py' in s.get('run', ''))
        self.assertIn("DCIM_COLLABORATION_ENABLED == 'true'", seal['if'])

    def setUp(self):
        self.head, self.base = 'a'*40, 'b'*40
        self.policy = dict(enabled=True, repository='o/r', default_branch='main', owner={'id':1},
            issuer=dict(id=2,login='claude[bot]',app_id=3,app_slug='claude'),
            providers={'opencode':{'identity':dict(id=4,login='native[bot]',app_id=5,app_slug='native')}})
        self.pr = dict(number=211,state='open',head={'sha':self.head,'repo':{'full_name':'o/r'}},base={'sha':self.base,'ref':'main','repo':{'full_name':'o/r'}})
        self.root = dict(id=10,user={'id':1,'type':'User'},author_association='OWNER',body='/oc review',created_at='one',updated_at='one')
        self.command = dict(id=11,user={'id':2,'login':'claude[bot]','type':'Bot'},performed_via_github_app={'id':3,'slug':'claude'},
            body='/oc review independently\nDCIM-Task: pr-211-root-10\nDCIM-Delivery: '+'c'*32+'\nDCIM-Root: 10\nDCIM-Head: '+self.head,
            issue_url='https://api.github.com/repos/o/r/issues/211',created_at='two',updated_at='two')
        self.event = dict(action='created',issue={'number':211,'pull_request':{'url':'https://api.github.com/repos/o/r/pulls/211'}},comment=deepcopy(self.command))
        self.review = dict(id=20,commit_id=self.head,state='COMMENTED',user={'id':4,'login':'native[bot]','type':'Bot'},body='<!-- opencode-review:'+json.dumps(dict(reviewed_commit=self.head,base_commit=self.base,stale_on_arrival=False,run_url='https://github.com/o/r/actions/runs/30'))+' -->\nFindings')
        self.witness = dict(id=21,user={'id':4,'login':'native[bot]','type':'Bot'},performed_via_github_app={'id':5,'slug':'native'},created_at='three',updated_at='three')
        self.comments=[self.root,self.command]; self.posts=0; self.api_root='https://api.github.com/repos/o/r'
    def request(self,path,method='GET',body=None):
        if method=='POST':
            self.posts+=1; self.witness['body']=body['body']; return deepcopy(self.witness)
        return deepcopy({'/pulls/211':self.pr,'/issues/comments/11':self.command,'/pulls/211/reviews/20':self.review}[path])
    def pages(self,path): return deepcopy(self.comments)
    def seal(self): return adapter.seal(self,self.policy,self.event,20,self.head,self.base,30,1,self.base)
    def test_real_native_review_seals_execution_without_acceptance(self):
        value=self.seal()
        self.assertEqual(value['provider_comment_id'],21);self.assertEqual(value['native_review_id'],20)
        self.assertEqual(value['result'],'SUCCESS');self.assertNotIn('verdict',value)
        self.assertEqual(value['source'],dict(run_id=30,attempt=1,trust_sha=self.base))
    def test_wrong_source_app(self):
        self.command['performed_via_github_app']['id']=999
        self.event['comment']=deepcopy(self.command)
        with self.assertRaises(Rejected): self.seal()
        self.assertEqual(self.posts,0)
    def test_wrong_destination_app(self):
        self.witness['performed_via_github_app']['id']=999
        with self.assertRaises(Rejected): self.seal()
    def test_edited_or_changed_command(self):
        for field,value in [('updated_at','changed'),('body','/oc review spoof')]:
            with self.subTest(field=field):
                original=self.command[field];self.command[field]=value
                with self.assertRaises(Rejected): self.seal()
                self.command[field]=original
        self.assertEqual(self.posts,0)
    def test_stale_head_base_wrong_review_and_run(self):
        for obj,key,value in [(self.pr['head'],'sha','d'*40),(self.review,'commit_id','d'*40),(self.review,'state','APPROVED'),(self.review,'body','forged ACCEPT')]:
            with self.subTest(key=key):
                old=obj[key];obj[key]=value
                with self.assertRaises(Rejected): self.seal()
                obj[key]=old
        self.assertEqual(self.posts,0)
    def test_main_advance_preserves_original_native_execution_binding(self):
        self.pr['base']['sha']='d'*40
        value=self.seal()
        self.assertEqual(value['binding']['base'],self.base)
        self.assertEqual(value['source_sha'],self.base)
        self.review['body']=self.review['body'].replace('"stale_on_arrival": false','"stale_on_arrival": true')
        self.assertEqual(self.seal()['binding']['base'],self.base)
    def test_retry_recovers_same_fresh_app_witness(self):
        first=self.seal();self.comments.append(deepcopy(self.witness))
        self.assertEqual(self.seal(),first);self.assertEqual(self.posts,1)
        self.comments.append(deepcopy(self.witness))
        with self.assertRaises(Rejected):self.seal()
    def test_disabled_policy_rejects_before_writes(self):
        self.policy['enabled']=False
        with self.assertRaises(Rejected):self.seal()
        self.assertEqual(self.posts,0)
