"""Exercise immutable original execution provenance against mutable main."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'.github/scripts'))
import orchestration_provenance as p
from acceptance_core import Rejected, digest

OLD, NEW = 'a'*40, 'b'*40

class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((Path(__file__).resolve().parents[2]/p.POLICY).read_text())
        self.old_policy = deepcopy(self.policy)
        self.old = {'truncated':False,'tree':[
            {'path':'.github/workflows/opencode.yml','mode':'100644','type':'blob','sha':'c'*40},
            {'path':'.github/scripts/agent_authority.py','mode':'100644','type':'blob','sha':'d'*40},
            {'path':'docs/unrelated.md','mode':'100644','type':'blob','sha':'e'*40}]}
        self.current = deepcopy(self.old);self.current['tree'][-1]['sha']='f'*40
        self.compare = dict(base_commit={'sha':OLD},merge_base_commit={'sha':OLD},status='ahead',behind_by=0,total_commits=1,commits=[{'sha':NEW}])
        self.invalid = set();self.paths=[]
    def request(self, path):
        self.paths.append(path)
        if path.startswith('/commits/'):
            sha=path.rsplit('/',1)[-1]
            return {'sha':sha,'commit':{'verification':{'verified':sha not in self.invalid,'reason':'unsigned' if sha in self.invalid else 'valid'}}}
        if path.startswith('/compare/'):return deepcopy(self.compare)
        if path.startswith('/git/trees/'):return deepcopy(self.old if path.split('/')[3].split('?')[0]==OLD else self.current)
        raise AssertionError(path)
    def file(self,path,sha):
        assert path==p.POLICY and sha==OLD
        return json.dumps(self.old_policy).encode()
    def verify(self):return p.execution_policy(self,self.policy,OLD,NEW)
    def test_benign_main_advance_preserves_original_policy_and_provenance(self):
        self.assertEqual(self.verify(),self.old_policy)
        self.assertIn('/commits/'+OLD,self.paths);self.assertIn('/commits/'+NEW,self.paths)
    def test_same_revision_still_requires_verified_signature(self):
        self.invalid.add(OLD)
        with self.assertRaisesRegex(Rejected,'unverified'):p.execution_policy(self,self.policy,OLD,OLD)
    def test_unsigned_intervening_commit_rejected(self):
        middle='1'*40;self.compare['commits'].insert(0,{'sha':middle});self.compare['total_commits']=2;self.invalid.add(middle)
        with self.assertRaisesRegex(Rejected,'unverified'):self.verify()
    def test_revocation_overrides_reviewed_manifest(self):
        self.policy['execution_provenance']['revoked_revisions']=[OLD]
        self.policy['execution_provenance']['approved_manifests'][OLD]=digest(p.security_manifest(self,OLD))
        with self.assertRaisesRegex(Rejected,'revoked'):self.verify()
    def test_changed_security_source_requires_exact_reviewed_manifest(self):
        self.current['tree'][1]['sha']='1'*40
        with self.assertRaisesRegex(Rejected,'security-sensitive'):self.verify()
        self.policy['execution_provenance']['approved_manifests'][OLD]=digest(p.security_manifest(self,OLD))
        self.assertEqual(self.verify(),self.old_policy)
        self.old['tree'][0]['sha']='2'*40
        with self.assertRaisesRegex(Rejected,'security-sensitive'):self.verify()
    def test_authority_changes_cannot_be_overridden_by_manifest_approval(self):
        self.policy['owner']['id']=999
        self.policy['execution_provenance']['approved_manifests'][OLD]=digest(p.security_manifest(self,OLD))
        with self.assertRaisesRegex(Rejected,'authority changed'):self.verify()
    def test_observation_refresh_does_not_change_execution_authority(self):
        self.policy['providers']['claude']['proof']={'run_id':999}
        self.assertEqual(self.verify(),self.old_policy)
    def test_nonancestor_truncated_or_duplicate_lineage_rejected(self):
        for key,value in [('merge_base_commit',{'sha':'3'*40}),('behind_by',1),('status','diverged'),('total_commits',2),('commits',[{'sha':NEW},{'sha':NEW}])]:
            with self.subTest(key=key):
                old=self.compare[key];self.compare[key]=value
                with self.assertRaises(Rejected):self.verify()
                self.compare[key]=old
    def test_truncated_source_tree_and_symlinks_rejected(self):
        self.old['truncated']=True
        with self.assertRaisesRegex(Rejected,'incomplete'):self.verify()
        self.old['truncated']=False;self.old['tree'][1]['mode']='120000'
        with self.assertRaisesRegex(Rejected,'unsupported'):self.verify()
