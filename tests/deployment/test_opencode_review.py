"""IMPLEMENTER_REVIEWER_SEPARATION tests (tests v1.0). No network."""
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name):
    # Normal imports so the scripts and these tests share one module instance each; that is
    # what lets mock.patch of opencode_publish.Api take effect inside opencode_review.
    return importlib.import_module(name)


router = load('opencode_router')
sandbox = load('opencode_sandbox')
guard = load('opencode_guard')
review = load('opencode_review')
publish = load('opencode_publish')

SHIPPED_REGISTRY = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
SHIPPED_MATRIX = json.loads((ROOT / '.github/opencode/reviewer-capability-matrix.json').read_text())
BOT = 'dcim-opencode-agent[bot]'
SHA = 'a' * 40
NEWSHA = 'b' * 40
M = {name: f'opencode/{name}-free' for name in (
    'glm-5.3-flash', 'deepseek-v4.1-flash', 'mimo-v2.6-flash', 'minimax-m3.1-flash',
    'nemotron-3-ultra', 'nemotron-3.5-lightning')}
ALL = set(M.values())
REVIEW_META = {'reviewed_by': 'owner', 'date': '2026-10-21', 'source': 'https://opencode.ai/docs'}


def enabled_registry():
    registry = copy.deepcopy(SHIPPED_REGISTRY)
    for meta in registry['models'].values():
        meta.update(terms_verified=True, terms_review=dict(REVIEW_META), repo_code_approved=True,
                    operational_status='healthy')
    return registry


def evidence(score=0.8, **overrides):
    record = {'suite': 'seeded-defects', 'suite_version': '1', 'date': '2026-10-22',
              'run_url': 'https://github.com/o/r/actions/runs/1', 'results_sha256': 'e' * 64,
              'scores': {d: score for d in review.DIMENSIONS}}
    record.update(overrides)
    return record


def matrix_with(**scores):
    matrix = copy.deepcopy(SHIPPED_MATRIX)
    for key, score in scores.items():
        matrix['models'][M[key]]['evaluation'] = evidence(score) if score is not None else None
    return matrix


def impl_record(model='deepseek-v4.1-flash', session='ses_impl000111', commit=SHA):
    return {'commit': commit, 'model': M[model], 'session': session, 'run': '1'}


def bot_commit(model, session, sha=SHA, verified=True, reason='valid', login=BOT, mode='implement'):
    message = (f'opencode: x\n\nOpenCode-Run: https://x/1\nOpenCode-Model: {M[model]}\n'
               f'OpenCode-Session: {session}\nOpenCode-Mode: {mode}\nRequested-By: me')
    return {'sha': sha, 'author': {'login': login},
            'commit': {'message': message, 'verification': {'verified': verified, 'reason': reason}}}


class ReviewerSelectionTests(unittest.TestCase):
    registry = enabled_registry()

    def pick(self, matrix, records=None, available=ALL):
        return review.select_reviewer(self.registry, matrix, records or [], set(available))

    def test_different_models_for_implementation_and_review_allowed(self):
        matrix = matrix_with(**{'glm-5.3-flash': 0.7, 'minimax-m3.1-flash': 0.9})
        chosen = self.pick(matrix, [impl_record('deepseek-v4.1-flash')])
        self.assertEqual(chosen['model'], M['minimax-m3.1-flash'])
        review.check_separation(self.registry, [impl_record('deepseek-v4.1-flash')], chosen['model'], 'ses_review999')

    def test_strongest_verified_reviewer_wins_not_the_fastest(self):
        matrix = matrix_with(**{'glm-5.3-flash': 0.7, 'minimax-m3.1-flash': 0.9, 'mimo-v2.6-flash': 0.8})
        self.assertEqual(self.pick(matrix)['model'], M['minimax-m3.1-flash'])
        # speed or latency can never enter the ranking: such fields invalidate the evidence
        matrix['models'][M['glm-5.3-flash']]['evaluation'] = evidence(0.99, latency_ms=1)
        self.assertEqual(self.pick(matrix)['model'], M['minimax-m3.1-flash'])
        self.assertIn('no verified capability evidence',
                      self.pick(matrix)['excluded'][M['glm-5.3-flash']])

    def test_weighting_uses_the_version_controlled_priorities(self):
        matrix = matrix_with(**{'glm-5.3-flash': 0.0, 'minimax-m3.1-flash': 0.0})
        strong_security = {d: 0.5 for d in review.DIMENSIONS}
        strong_security['security_vulnerability_identification'] = 1.0
        strong_other = {d: 0.5 for d in review.DIMENSIONS}
        strong_other['complex_change_inspection'] = 1.0
        matrix['models'][M['glm-5.3-flash']]['evaluation'] = evidence(scores=strong_security)
        matrix['models'][M['minimax-m3.1-flash']]['evaluation'] = evidence(scores=strong_other)
        self.assertEqual(self.pick(matrix)['model'], M['glm-5.3-flash'])  # security weighs 0.25 vs 0.10

    def test_same_model_cannot_review_its_own_implementation(self):
        matrix = matrix_with(**{'deepseek-v4.1-flash': 0.99, 'glm-5.3-flash': 0.6})
        chosen = self.pick(matrix, [impl_record('deepseek-v4.1-flash')])
        self.assertEqual(chosen['model'], M['glm-5.3-flash'])
        self.assertIn('implemented this branch or PR', chosen['excluded'][M['deepseek-v4.1-flash']])
        with self.assertRaises(review.SeparationError):
            review.check_separation(self.registry, [impl_record('deepseek-v4.1-flash')],
                                    M['deepseek-v4.1-flash'], 'ses_review999')
        with self.assertRaises(review.SeparationError):  # also without the opencode/ prefix
            review.check_separation(self.registry, [impl_record('deepseek-v4.1-flash')],
                                    'deepseek-v4.1-flash-free', 'ses_review999')

    def test_every_prior_implementer_on_the_pr_is_excluded(self):
        matrix = matrix_with(**{'deepseek-v4.1-flash': 0.9, 'glm-5.3-flash': 0.8, 'mimo-v2.6-flash': 0.7})
        records = [impl_record('deepseek-v4.1-flash'), impl_record('glm-5.3-flash', 'ses_other00001', NEWSHA)]
        self.assertEqual(self.pick(matrix, records)['model'], M['mimo-v2.6-flash'])

    def test_different_models_with_the_same_session_rejected(self):
        for reviewer_session in ('ses_impl000111', '', None, 'bad session!'):
            with self.assertRaises(review.SeparationError, msg=str(reviewer_session)):
                review.check_separation(self.registry, [impl_record()], M['glm-5.3-flash'], reviewer_session)

    def test_unavailable_reviewer_selects_eligible_alternative(self):
        matrix = matrix_with(**{'minimax-m3.1-flash': 0.9, 'glm-5.3-flash': 0.7})
        chosen = self.pick(matrix, available=ALL - {M['minimax-m3.1-flash']})
        self.assertEqual(chosen['model'], M['glm-5.3-flash'])
        self.assertIn('not in live catalog', chosen['excluded'][M['minimax-m3.1-flash']])

    def test_no_eligible_reviewer_is_review_blocked(self):
        cases = {
            'no evidence anywhere': (SHIPPED_MATRIX, [], ALL),
            'only the implementer is evaluated': (matrix_with(**{'deepseek-v4.1-flash': 0.9}),
                                                  [impl_record('deepseek-v4.1-flash')], ALL),
            'evaluated model unavailable': (matrix_with(**{'glm-5.3-flash': 0.9}), [], ALL - {M['glm-5.3-flash']}),
            'below the per-dimension floor': (matrix_with(**{'glm-5.3-flash': 0.4}), [], ALL),
        }
        for name, (matrix, records, available) in cases.items():
            with self.assertRaises(review.ReviewBlocked, msg=name):
                self.pick(matrix, records, available)

    def test_data_policy_and_health_still_apply_to_reviewers(self):
        registry = enabled_registry()
        registry['models'][M['glm-5.3-flash']]['terms_verified'] = False
        registry['models'][M['minimax-m3.1-flash']]['operational_status'] = 'degraded'
        registry['models'][M['mimo-v2.6-flash']]['repo_code_approved'] = False
        matrix = matrix_with(**{'glm-5.3-flash': 0.99, 'minimax-m3.1-flash': 0.98, 'mimo-v2.6-flash': 0.97,
                                'nemotron-3-ultra': 0.5})
        chosen = review.select_reviewer(registry, matrix, [], ALL)
        self.assertEqual(chosen['model'], M['nemotron-3-ultra'])
        registry['models'][M['nemotron-3-ultra']]['repo_code_approved'] = False
        with self.assertRaises(review.ReviewBlocked):
            review.select_reviewer(registry, matrix, [], ALL)

    def test_evidence_must_be_complete_and_reproducible(self):
        bad = [evidence(run_url='http://insecure'), evidence(results_sha256='nothex'), evidence(suite=''),
               evidence(scores={d: 0.9 for d in review.DIMENSIONS[:-1]}),
               evidence(scores={**{d: 0.9 for d in review.DIMENSIONS}, 'complex_change_inspection': 1.5}),
               evidence(scores={**{d: 0.9 for d in review.DIMENSIONS}, 'complex_change_inspection': True}),
               'fast', None]
        for record in bad:
            matrix = matrix_with()
            matrix['models'][M['glm-5.3-flash']]['evaluation'] = record
            with self.assertRaises(review.ReviewBlocked, msg=str(record)[:40]):
                self.pick(matrix)

    def test_shipped_matrix_claims_no_model_is_better(self):
        for name, entry in SHIPPED_MATRIX['models'].items():
            self.assertIsNone(entry['evaluation'], name)
        self.assertEqual(set(SHIPPED_MATRIX['dimensions']), set(review.DIMENSIONS))
        self.assertAlmostEqual(sum(SHIPPED_MATRIX['dimensions'].values()), 1.0)
        with self.assertRaises(review.ReviewBlocked):
            review.select_reviewer(enabled_registry(), SHIPPED_MATRIX, [], ALL)

    def test_malformed_matrix_blocks_instead_of_guessing(self):
        for mutate in (lambda m: m['dimensions'].pop('code_reasoning_correctness'),
                       lambda m: m['dimensions'].update(code_reasoning_correctness=0.9),
                       lambda m: m.update(minimum_dimension_score=2), lambda m: m.pop('models')):
            matrix = copy.deepcopy(SHIPPED_MATRIX)
            mutate(matrix)
            with self.assertRaises(review.ReviewBlocked):
                review.select_reviewer(enabled_registry(), matrix, [], ALL)


class SeparationPolicyTests(unittest.TestCase):
    def test_policy_is_present_and_every_field_is_enforced(self):
        policy = SHIPPED_REGISTRY['agent_separation']
        self.assertEqual(policy, router.SEPARATION_POLICY)
        self.assertEqual(router.separation_policy(SHIPPED_REGISTRY), policy)
        for key in policy:
            weakened = copy.deepcopy(SHIPPED_REGISTRY)
            weakened['agent_separation'][key] = False if isinstance(policy[key], bool) else 'other'
            with self.assertRaises(router.RoutingError, msg=key):
                router.load_registry(weakened)
            with self.assertRaises(router.RoutingError, msg=key):  # implementation routing fails closed too
                router.route('fix a typo', weakened, ALL)
        missing = copy.deepcopy(SHIPPED_REGISTRY)
        del missing['agent_separation']
        with self.assertRaises(router.RoutingError):
            router.load_registry(missing)
        extra = copy.deepcopy(SHIPPED_REGISTRY)
        extra['agent_separation']['unused_flag'] = True
        with self.assertRaises(router.RoutingError):
            router.load_registry(extra)

    def test_policy_fields_are_all_read_by_review_code(self):
        source = (SCRIPTS / 'opencode_review.py').read_text() + (SCRIPTS / 'opencode_router.py').read_text()
        for key in ('require_distinct_models', 'require_distinct_sessions', 'review_read_only'):
            self.assertIn(key, source)


class ImplementerRecordTests(unittest.TestCase):
    def test_records_come_only_from_verified_commits_by_the_app_bot(self):
        commits = [bot_commit('glm-5.3-flash', 'ses_aaa111'),
                   bot_commit('mimo-v2.6-flash', 'ses_bbb222', verified=False, reason='unsigned'),
                   bot_commit('minimax-m3.1-flash', 'ses_ccc333', login='someone'),
                   bot_commit('deepseek-v4.1-flash', 'ses_ddd444', mode='review')]
        records = review.implementer_records(commits, BOT)
        self.assertEqual([r['model'] for r in records], [M['glm-5.3-flash']])
        self.assertEqual(records[0]['session'], 'ses_aaa111')
        self.assertEqual(review.implementer_records('junk', BOT), [])

    def test_publisher_writes_model_and_session_trailers(self):
        class Api:
            def __init__(self):
                self.calls = []
            def call(self, method, path, body=None):
                self.calls.append((method, path, body))
                if path == '':
                    return {'default_branch': 'main', 'full_name': 'o/r'}
                if path.startswith('branches/main'):
                    return {'protected': True}
                if path.startswith('issues/'):
                    return {'number': 7, 'state': 'open'}
                if path.startswith('compare/'):
                    return {'status': 'behind'}
                if path.startswith('commits/'):
                    return {'commit': {'verification': {'verified': True, 'reason': 'valid'}}, 'author': {'login': BOT}}
                if path == 'pulls':
                    return {'number': 3, 'html_url': 'https://x'}
                return {}
            def graphql(self, query, variables):
                self.calls.append(('GRAPHQL', '', variables))
                return {'createCommitOnBranch': {'commit': {'oid': NEWSHA}}}
        env = {'GITHUB_REPOSITORY': 'o/r', 'DEFAULT_BRANCH': 'main', 'EVENT_KIND': 'issue', 'TARGET_NUMBER': '7',
               'AUTHORIZED_SHA': SHA, 'GENERATION_STATUS': 'success', 'RUN_URL': 'https://x', 'RUN_ID': '1',
               'RUN_ATTEMPT': '1', 'ACTOR_LOGIN': 'me', 'EXPECT_BOT_LOGIN': BOT, 'TASK': 't', 'MODEL': M['glm-5.3-flash'],
               'CATEGORY': 'general', 'ROUTE_REASON': 'r', 'PR_HEAD_REF': '', 'SESSION_ID': 'ses_impl000111'}
        api = Api()
        publish.publish(api, env, {'base_sha': SHA, 'additions': [{'path': 'a', 'contents': 'eA=='}], 'deletions': []},
                        sleep=lambda _s: None)
        message = next(c for c in api.calls if c[0] == 'GRAPHQL')[2]['input']['message']['body']
        parsed = review.implementer_records([{'sha': NEWSHA, 'author': {'login': BOT}, 'commit': {
            'message': 'h\n\n' + message, 'verification': {'verified': True, 'reason': 'valid'}}}], BOT)
        self.assertEqual((parsed[0]['model'], parsed[0]['session']), (M['glm-5.3-flash'], 'ses_impl000111'))
        comment = next(c for c in api.calls if c[1].endswith('/comments'))[2]['body']
        for expected in (M['glm-5.3-flash'], 'ses_impl000111', NEWSHA, 'opencode/issue7-1-1'):
            self.assertIn(expected, comment)


class ReviewOutputTests(unittest.TestCase):
    GOOD = {'verdict': 'blocking_findings', 'summary': 'Looks risky.', 'findings': [
        {'severity': 'high', 'title': 'Unchecked transaction', 'files': ['backend/app/db.py'],
         'evidence': 'pytest tests/db -k rollback fails', 'recommendation': 'wrap in a transaction'},
        {'severity': 'low', 'title': 'Naming', 'files': [], 'evidence': '', 'recommendation': 'rename'}],
        'verification': [{'command': 'pytest -q', 'result': '1 failed'}]}

    def wrap(self, data):
        return 'Thoughts...\n```json\n' + json.dumps(data) + '\n```'

    def test_findings_carry_severity_files_evidence_and_recommendation(self):
        parsed = review.parse_review(self.wrap(self.GOOD))
        self.assertEqual(parsed['verdict'], 'BLOCKING_FINDINGS')
        first = parsed['findings'][0]
        self.assertEqual((first['severity'], first['files']), ('high', ['backend/app/db.py']))
        self.assertTrue(first['evidence'] and first['recommendation'])
        body = review.render_review(parsed, {'commit': SHA, 'base_commit': BASE_C, 'reviewer_model': M['glm-5.3-flash'],
                                             'reviewer_session': 'ses_review999', 'records': [impl_record()],
                                             'branch': 'feature/x', 'run_url': 'https://x'})
        for expected in ('[HIGH]', 'backend/app/db.py', 'Evidence:', 'Recommendation:', SHA, 'ses_review999',
                         'ses_impl000111', M['deepseek-v4.1-flash'], 'feature/x', 'Verification evidence',
                         'External acceptance by Codex or Jules is still required', 'not an approval'):
            self.assertIn(expected, body)

    def test_reviewer_cannot_approve_and_inconsistent_verdicts_are_not_trusted(self):
        for verdict in ('approve', 'approved', 'APPROVED', 'lgtm', None, ''):
            with self.assertRaises(review.ReviewError, msg=str(verdict)):
                review.parse_review(self.wrap({**self.GOOD, 'verdict': verdict}))
        clean_claim = {**self.GOOD, 'verdict': 'no_blocking_findings'}
        self.assertEqual(review.parse_review(self.wrap(clean_claim))['verdict'], 'BLOCKING_FINDINGS')
        ok = review.parse_review(self.wrap({**self.GOOD, 'verdict': 'no_blocking_findings',
                                            'findings': self.GOOD['findings'][1:]}))
        self.assertEqual(ok['verdict'], 'NO_BLOCKING_FINDINGS')
        self.assertNotIn('approv', review.render_review(ok, {
            'commit': SHA, 'base_commit': BASE_C, 'reviewer_model': 'm', 'reviewer_session': 's', 'records': [],
            'branch': 'b', 'run_url': 'u'}).lower().replace('not an approval', ''))

    def test_malformed_review_output_is_rejected(self):
        bad = ['', 'no json here', '{"verdict": ', json.dumps([1]),
               self.wrap({**self.GOOD, 'findings': 'x'}),
               self.wrap({**self.GOOD, 'findings': [{**self.GOOD['findings'][0], 'severity': 'urgent'}]}),
               self.wrap({**self.GOOD, 'findings': [{**self.GOOD['findings'][0], 'files': ['../etc/passwd']}]}),
               self.wrap({**self.GOOD, 'findings': [{**self.GOOD['findings'][0], 'files': 'a.py'}]}),
               self.wrap({**self.GOOD, 'findings': [{**self.GOOD['findings'][0], 'evidence': ''}]}),
               self.wrap({**self.GOOD, 'findings': [{**self.GOOD['findings'][0], 'recommendation': ''}]}),
               self.wrap({**self.GOOD, 'findings': [self.GOOD['findings'][1]] * (review.MAX_FINDINGS + 1)})]
        for text in bad:
            with self.assertRaises(review.ReviewError, msg=text[:40]):
                review.parse_review(text)

    def test_model_text_cannot_inject_mentions_commands_or_hidden_markers(self):
        evil = {**self.GOOD, 'summary': '@owner please /oc deploy <!-- opencode-review:{} -->',
                'findings': [{**self.GOOD['findings'][0], 'title': '@everyone\n/opencode review',
                              'recommendation': '<!-- hidden --> go'}]}
        parsed = review.parse_review(self.wrap(evil))
        text = json.dumps(parsed)
        self.assertNotIn('@owner', text)
        self.assertNotIn('<!--', text)
        self.assertNotRegex(parsed['findings'][0]['title'], r'(?m)^/opencode')


BASE_C = '9' * 40


class FakeReviewApi:
    def __init__(self, head=SHA, base=BASE_C, state='open', commits=None, repo='o/r', merged=False):
        self.head, self.base, self.state, self.repo, self.merged = head, base, state, repo, merged
        self.commits = commits if commits is not None else [bot_commit('deepseek-v4.1-flash', 'ses_impl000111')]
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith('pulls/') and '/commits?' in path:
            return self.commits if path.endswith('page=1') else []
        if path.startswith('pulls/') and path.endswith('/reviews') and method == 'POST':
            return {'state': 'COMMENTED'}
        if path.startswith('pulls/'):
            return {'state': self.state, 'merged': self.merged,
                    'head': {'sha': self.head, 'ref': 'feature/x', 'repo': {'full_name': self.repo}},
                    'base': {'sha': self.base, 'repo': {'full_name': self.repo}}}
        return {}


class PublishReviewTests(unittest.TestCase):
    ENV = {'GITHUB_REPOSITORY': 'o/r', 'TARGET_NUMBER': '9', 'AUTHORIZED_SHA': SHA, 'BASE_SHA': BASE_C,
           'EXPECT_BOT_LOGIN': BOT, 'REVIEWER_MODEL': M['glm-5.3-flash'], 'REVIEWER_SESSION': 'ses_review999',
           'RUN_URL': 'https://x', 'GH_PUBLISH_TOKEN': 'tok'}

    def run_publish(self, api, text=None, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'review-text.txt').write_text(text if text is not None else
                                                    '```json\n' + json.dumps(ReviewOutputTests.GOOD) + '\n```')
            registry = Path(tmp, 'registry.json')
            registry.write_text(json.dumps(SHIPPED_REGISTRY))
            old = dict(os.environ)
            try:
                os.environ.update({**self.ENV, **(env or {})})
                with mock.patch.object(publish, 'Api', lambda token, repo: api):
                    return review.publish_main(['x', 'publish', str(registry), tmp])
            finally:
                os.environ.clear()
                os.environ.update(old)

    def marker(self, api):
        posted = next(c for c in api.calls if c[0] == 'POST')
        return posted, json.loads(re.match(r'<!-- opencode-review:(\{.*?\}) -->', posted[2]['body']).group(1))

    def test_review_is_comment_only_and_tied_to_both_reviewed_commits(self):
        api = FakeReviewApi()
        self.run_publish(api)
        posted, marker = self.marker(api)
        self.assertEqual(posted[1], 'pulls/9/reviews')
        self.assertEqual(posted[2]['event'], 'COMMENT')  # never APPROVE or REQUEST_CHANGES
        self.assertEqual(posted[2]['commit_id'], SHA)
        self.assertEqual(marker['reviewed_commit'], SHA)
        self.assertEqual(marker['base_commit'], BASE_C)
        self.assertIs(marker['stale_on_arrival'], False)
        self.assertEqual(marker['external_acceptance'], 'REQUIRED')
        self.assertEqual(marker['implementer_models'], [M['deepseek-v4.1-flash']])
        self.assertEqual(marker['reviewer_model'], M['glm-5.3-flash'])
        self.assertNotIn('STALE ON ARRIVAL', posted[2]['body'])

    def test_review_findings_cannot_trigger_merge_or_approval(self):
        api = FakeReviewApi()
        self.run_publish(api)
        self.assertEqual({(m, p) for m, p, _ in api.calls if m != 'GET'}, {('POST', 'pulls/9/reviews')})
        for name in ('opencode_review.py', 'opencode_publish.py', 'opencode_gate.py'):
            source = (SCRIPTS / name).read_text()
            self.assertNotRegex(source, r"/merge|merge_pull|auto_merge|'APPROVE'|\"APPROVE\"|REQUEST_CHANGES")
        workflow = (ROOT / '.github/workflows/opencode.yml').read_text()
        self.assertNotRegex(workflow, r'gh pr merge|auto-merge|/merge|enable_pr_auto_merge')

    def test_same_model_and_session_blocked_at_publication_time(self):
        for env in ({'REVIEWER_MODEL': M['deepseek-v4.1-flash']}, {'REVIEWER_SESSION': 'ses_impl000111'},
                    {'REVIEWER_SESSION': ''}):
            api = FakeReviewApi()
            with self.assertRaises(review.SeparationError):
                self.run_publish(api, env=env)
            self.assertEqual([c for c in api.calls if c[0] == 'POST'], [])

    def test_pr_changed_before_publication_is_posted_marked_stale_bound_to_the_reviewed_revisions(self):
        for kwargs in ({'head': NEWSHA}, {'base': 'c' * 40}, {'state': 'closed'}, {'merged': True}):
            api = FakeReviewApi(**kwargs)
            self.run_publish(api)
            posted, marker = self.marker(api)
            self.assertIs(marker['stale_on_arrival'], True, kwargs)
            self.assertIn('STALE ON ARRIVAL', posted[2]['body'])
            self.assertEqual(posted[2]['commit_id'], SHA)          # still the commit that was reviewed
            self.assertEqual(marker['reviewed_commit'], SHA)
            self.assertEqual(marker['base_commit'], BASE_C)
            status = review.acceptance_status([{
                'user': {'login': BOT}, 'commit_id': SHA, 'submitted_at': '2026-10-22T10:00:00Z',
                'body': posted[2]['body']}], SHA, BOT, BASE_C)
            self.assertEqual(status['status'], 'STALE', kwargs)    # stale even though SHAs now match

    def test_foreign_repository_pr_is_refused(self):
        api = FakeReviewApi(repo='evil/r')
        with self.assertRaises(review.ReviewError):
            self.run_publish(api)
        self.assertEqual([c for c in api.calls if c[0] == 'POST'], [])

    def test_invalid_review_output_is_not_posted(self):
        api = FakeReviewApi()
        with self.assertRaises(review.ReviewError):
            self.run_publish(api, text='APPROVED, looks good')
        self.assertEqual([c for c in api.calls if c[0] == 'POST'], [])


class AcceptanceStalenessTests(unittest.TestCase):
    def posted(self, commit, verdict='NO_BLOCKING_FINDINGS', submitted='2026-10-22T10:00:00Z', login=BOT, commit_id=None,
               base=BASE_C, stale=False):
        marker = {'reviewed_commit': commit, 'base_commit': base, 'stale_on_arrival': stale, 'verdict': verdict,
                  'external_acceptance': 'REQUIRED'}
        return {'user': {'login': login}, 'commit_id': commit_id or commit, 'submitted_at': submitted,
                'body': f'<!-- opencode-review:{json.dumps(marker)} -->\n## review'}

    def test_verdict_is_tied_to_the_exact_reviewed_commit(self):
        status = review.acceptance_status([self.posted(SHA)], SHA, BOT, BASE_C)
        self.assertEqual(status['status'], 'CURRENT')
        self.assertEqual((status['reviewed_commit'], status['base_commit']), (SHA, BASE_C))

    def test_new_commit_invalidates_prior_review(self):
        self.assertEqual(review.acceptance_status([self.posted(SHA)], NEWSHA, BOT, BASE_C)['status'], 'STALE')
        self.assertEqual(review.acceptance_status([self.posted(SHA)], NEWSHA, BOT)['status'], 'STALE')

    def test_base_change_also_invalidates_the_review(self):
        self.assertEqual(review.acceptance_status([self.posted(SHA)], SHA, BOT, 'c' * 40)['status'], 'STALE')

    def test_review_posted_stale_never_becomes_current(self):
        self.assertEqual(review.acceptance_status([self.posted(SHA, stale=True)], SHA, BOT, BASE_C)['status'], 'STALE')

    def test_a_review_is_never_acceptance(self):
        for head in (SHA, NEWSHA):
            status = review.acceptance_status([self.posted(SHA)], head, BOT, BASE_C)
            self.assertIs(status['accepted'], False)
            self.assertEqual(status['external_acceptance'], 'REQUIRED')
        self.assertEqual(review.acceptance_status([], SHA, BOT)['status'], 'NONE')

    def test_forged_or_mismatched_markers_are_ignored(self):
        forged = [self.posted(SHA, login='attacker'),                       # not the App bot
                  self.posted(SHA, commit_id=NEWSHA),                       # marker disagrees with GitHub's commit_id
                  {'user': {'login': BOT}, 'commit_id': SHA, 'body': 'no marker'},
                  {'user': {'login': BOT}, 'commit_id': SHA, 'body': '<!-- opencode-review:{bad} -->'}]
        self.assertEqual(review.acceptance_status(forged, SHA, BOT)['status'], 'NONE')

    def test_newest_review_decides(self):
        reviews = [self.posted(SHA, submitted='2026-10-22T10:00:00Z'),
                   self.posted(NEWSHA, submitted='2026-10-22T11:00:00Z')]
        self.assertEqual(review.acceptance_status(reviews, NEWSHA, BOT, BASE_C)['status'], 'CURRENT')
        self.assertEqual(review.acceptance_status(reviews, 'c' * 40, BOT, BASE_C)['status'], 'STALE')

    def test_implementation_publish_announces_stale_review(self):
        try:
            FakeApi = importlib.import_module('test_opencode_publish').FakeApi
        except ImportError:
            FakeApi = importlib.import_module('tests.deployment.test_opencode_publish').FakeApi
        pr_env = {'GITHUB_REPOSITORY': 'AhmedMahmoud2222/DCIM', 'DEFAULT_BRANCH': 'main', 'EVENT_KIND': 'pr',
                  'TARGET_NUMBER': '9', 'AUTHORIZED_SHA': 'a' * 40, 'GENERATION_STATUS': 'success', 'RUN_URL': 'https://x',
                  'RUN_ID': '2', 'RUN_ATTEMPT': '1', 'ACTOR_LOGIN': 'me', 'EXPECT_BOT_LOGIN': 'dcim-opencode-agent[bot]',
                  'TASK': 't', 'MODEL': M['glm-5.3-flash'], 'CATEGORY': 'general', 'ROUTE_REASON': 'r',
                  'PR_HEAD_REF': 'feature/x', 'SESSION_ID': 'ses_impl000222'}
        api = FakeApi()
        api.reviews = [self.posted(SHA)]
        publish.publish(api, pr_env, {'base_sha': 'a' * 40, 'additions': [{'path': 'a', 'contents': 'eA=='}],
                                      'deletions': []}, sleep=lambda _s: None, git_push=api.push)
        comment = next(c for c in api.calls if c[1] == 'issues/9/comments')[2]['body']
        self.assertIn('STALE', comment)
        self.assertIn(SHA[:12], comment)


class ReadOnlyAndConfidentialityTests(unittest.TestCase):
    def test_reviewer_config_denies_every_editing_path(self):
        config = sandbox.agent_config(M['glm-5.3-flash'], 1234, 'review')
        self.assertEqual(config['permission']['edit'], 'deny')
        self.assertEqual(sandbox.agent_config(M['glm-5.3-flash'], 1234)['permission']['edit'], 'allow')
        for tool in ('webfetch', 'websearch', 'task', 'external_directory'):
            self.assertEqual(config['permission'][tool], 'deny')
        with self.assertRaises(sandbox.SandboxError):
            sandbox.agent_config(M['glm-5.3-flash'], 1, 'admin')

    def test_reviewer_modifications_are_detected(self):
        digest = hashlib.sha256(b'diff').hexdigest()
        clean = {'changes': [{'path': 'REVIEW/diff.patch', 'op': 'add', 'sha256': digest}], 'unsupported': []}
        review.assert_read_only(clean, digest)
        review.assert_read_only({'changes': [], 'unsupported': []}, digest)
        tampered = [
            {'changes': [{'path': 'backend/app.py', 'op': 'modify', 'sha256': 'x'}], 'unsupported': []},
            {'changes': [{'path': 'new.py', 'op': 'add', 'sha256': 'x'}], 'unsupported': []},
            {'changes': [{'path': 'a.py', 'op': 'delete'}], 'unsupported': []},
            {'changes': [{'path': 'REVIEW/diff.patch', 'op': 'add', 'sha256': 'other'}], 'unsupported': []},
            {'changes': [], 'unsupported': [{'path': 'link', 'reason': 'x'}]},
        ]
        for manifest in tampered:
            with self.assertRaises(review.ReviewError):
                review.assert_read_only(manifest, digest)

    def test_workflow_builds_a_root_controlled_tree_and_probes_as_the_reviewer_before_the_agent_starts(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/opencode.yml').read_text())
        steps = {s.get('name'): s for s in workflow['jobs']['review']['steps']}
        layout = steps['Create isolated agent user and root-controlled read-only source']['run']
        for required in ('fsisolate.py build "${RUNNER_TEMP}/stage" /srv/ocreview', 'fsisolate.py mount /srv/ocreview',
                         'fsisolate.py chain /srv/ocreview/src', 'fsisolate.py digest /srv/ocreview/src',
                         'sudo -u ocagent python3 -I /opt/opencode/fsisolate.py probe /srv/ocreview/src'):
            self.assertIn(required, layout)
        self.assertNotIn('chown -R ocagent', layout)
        order = [s.get('name') for s in workflow['jobs']['review']['steps']]
        run_name = 'Run reviewer (loopback-only, read-only, model-bound proxy)'
        self.assertLess(order.index('Create isolated agent user and root-controlled read-only source'), order.index(run_name))
        run = steps[run_name]['run']
        self.assertIn('config "${MODEL}" "${port}" review', run)
        self.assertIn('/srv/ocreview/src 25', run)
        self.assertIn('implementers.json', run)          # reviewer proxy is told which models are forbidden
        self.assertEqual(steps[run_name]['env']['GUARD_MODE'], 'review')
        after = steps['Verify separation, unchanged source and effective permissions']
        for required in ('opencode_review.py check', 'fsisolate.py chain /srv/ocreview/src', 'EXPECTED_DIGEST',
                         'fsisolate.py probe /srv/ocreview/src', 'verify-readonly'):
            self.assertIn(required, after['run'])
        self.assertEqual(after['env']['EXPECTED_DIGEST'], '${{ steps.layout.outputs.tree_digest }}')

    def test_reviewer_job_has_no_write_rights_and_a_review_only_publish_token(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/opencode.yml').read_text())
        self.assertEqual(workflow['jobs']['review']['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        text = yaml.dump(workflow['jobs']['review'])
        for banned in ('create-github-app-token', 'APP_PRIVATE_KEY', 'GH_PUBLISH_TOKEN', 'pulls/', 'git push'):
            self.assertNotIn(banned, text)
        mint = next(s for s in workflow['jobs']['review_publish']['steps'] if 'create-github-app-token' in s.get('uses', ''))
        self.assertEqual({k: v for k, v in mint['with'].items() if k.startswith('permission-')},
                         {'permission-pull-requests': 'write'})

    def compare_fetch(self, diff, files, commits=None, total=None, merge_base=True):
        calls = []

        def fetch(path, accept=None):
            calls.append((path, accept))
            if accept:
                return diff
            return {'merge_base_commit': {'sha': BASE_C} if merge_base else None,
                    'files': [{'filename': f} if isinstance(f, str) else f for f in files],
                    'commits': commits if commits is not None else [bot_commit('glm-5.3-flash', 'ses_aaa111')],
                    'total_commits': total if total is not None else len(commits or [1])}
        fetch.calls = calls
        return fetch

    def prepare(self, fetch):
        with tempfile.TemporaryDirectory() as tmp:
            result = review.prepare_inputs(fetch, 'o/r', '9', SHA, BASE_C, BOT, tmp)
            return result, Path(tmp, 'diff.patch').exists()

    def test_restricted_data_never_reaches_the_reviewer(self):
        secret = 'ghp_' + 'Z9y8X7w6' * 5
        cases = {
            'credential-like file': ('diff --git a/.env b/.env\n+A=1\n', ['.env']),
            'untrusted config': ('diff --git a/.opencode/x.md b/.opencode/x.md\n+x\n', ['.opencode/x.md']),
            'secret in diff': (f'diff --git a/a.py b/a.py\n+token = "{secret}"\n', ['a.py']),
            'oversized': ('diff --git a/a.py b/a.py\n' + '+x\n' * 600_000, ['a.py']),
            'diff names a path GitHub does not list': ('diff --git a/a.py b/a.py\n+x\n', ['b.py']),
            'files list possibly truncated': ('diff --git a/a.py b/a.py\n+x\n', [f'f{i}.py' for i in range(300)]),
        }
        for name, (diff, files) in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(review.ReviewBlocked, msg=name):
                    review.prepare_inputs(self.compare_fetch(diff, files), 'o/r', '9', SHA, BASE_C, BOT, tmp)
                self.assertFalse(os.path.exists(os.path.join(tmp, 'diff.patch')), name)

    def test_review_material_comes_from_the_two_immutable_revisions_not_the_live_pr(self):
        fetch = self.compare_fetch('diff --git a/backend/a.py b/backend/a.py\n+x = 1\n', ['backend/a.py'])
        result, wrote = self.prepare(fetch)
        self.assertTrue(wrote)
        self.assertEqual((result['base'], result['head']), (BASE_C, SHA))
        for path, _accept in fetch.calls:
            self.assertIn(f'compare/{BASE_C}...{SHA}', path)       # never pulls/9 (which can move)
            self.assertNotIn('/pulls/', path)

    def test_missing_or_malformed_revision_ids_block_the_review(self):
        for base, head in (('', SHA), (BASE_C, ''), ('main', SHA), (BASE_C, 'HEAD'), ('A' * 40, SHA), (BASE_C, 'A' * 40)):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(review.ReviewBlocked, msg=f'{base!r} {head!r}'):
                    review.prepare_inputs(self.compare_fetch('', []), 'o/r', '9', head, base, BOT, tmp)

    def test_unrelated_revisions_and_unbounded_commit_lists_block_the_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(review.ReviewBlocked):
                review.prepare_inputs(self.compare_fetch('', [], merge_base=False), 'o/r', '9', SHA, BASE_C, BOT, tmp)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(review.ReviewBlocked):
                review.prepare_inputs(self.compare_fetch('diff --git a/a b/a\n', ['a'], commits=[bot_commit('glm-5.3-flash', 'ses_aaa111')],
                                                         total=5000), 'o/r', '9', SHA, BASE_C, BOT, tmp)

    def test_git_quoted_paths_are_decoded_and_checked(self):
        self.assertEqual(review.unquote_git_path('"dir/caf\\303\\251.py"'), 'dir/caf\u00e9.py')
        self.assertEqual(review.unquote_git_path('"a\\tb\\"c"'), 'a\tb"c')
        self.assertEqual(review.unquote_git_path('plain/path.py'), 'plain/path.py')
        with self.assertRaises(review.ReviewBlocked):
            review.unquote_git_path('"bad\\q"')
        diff = ('diff --git "a/dir/caf\\303\\251.py" "b/dir/caf\\303\\251.py"\n--- "a/dir/caf\\303\\251.py"\n+++ "b/dir/caf\\303\\251.py"\n+x\n'
                'diff --git a/with space.txt b/with space.txt\n--- a/with space.txt\n+++ b/with space.txt\n+y\n'
                'diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt\n')
        self.assertEqual(review.diff_paths(diff), {'dir/caf\u00e9.py', 'with space.txt', 'old.txt', 'new.txt'})

    def test_octal_escaped_names_cannot_hide_a_credential_file(self):
        hidden = '"dir/\\056env"'          # git would never quote this, but the decoder must still see .env
        diff = f'diff --git a/{hidden} b/{hidden}\n--- {hidden}\n+++ {hidden}\n+A=1\n'
        self.assertIn('dir/.env', review.diff_paths(diff))
        with self.assertRaises(review.ReviewBlocked):
            self.prepare(self.compare_fetch(diff, ['dir/.env']))
        # a quoted unicode path is accepted when GitHub lists the same decoded name
        quoted = 'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n+x\n'
        result, _ = self.prepare(self.compare_fetch(quoted, ['caf\u00e9.py']))
        self.assertEqual(result['files'], ['caf\u00e9.py'])
        # a rename INTO a credential file name is caught through the rename lines
        rename = 'diff --git a/x.txt b/.env\nrename from x.txt\nrename to .env\n'
        with self.assertRaises(review.ReviewBlocked):
            self.prepare(self.compare_fetch(rename, [{'filename': '.env', 'previous_filename': 'x.txt'}]))

    def test_clean_diff_is_prepared_with_implementer_records(self):
        fetch = self.compare_fetch('diff --git a/backend/a.py b/backend/a.py\n+x = 1\n', ['backend/a.py'],
                                   commits=[bot_commit('glm-5.3-flash', 'ses_aaa111')], total=1)
        with tempfile.TemporaryDirectory() as tmp:
            result = review.prepare_inputs(fetch, 'o/r', '9', SHA, BASE_C, BOT, tmp)
            self.assertEqual(result['files'], ['backend/a.py'])
            self.assertEqual(json.loads(Path(tmp, 'implementers.json').read_text())[0]['session'], 'ses_aaa111')

    def test_sensitive_review_focus_is_blocked_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for name, data in (('registry.json', enabled_registry()), ('matrix.json', matrix_with(**{'glm-5.3-flash': 0.9})),
                               ('available.json', {'data': [{'id': i} for i in ALL]}), ('impl.json', [])):
                files[name] = Path(tmp, name)
                files[name].write_text(json.dumps(data))
            out = Path(tmp, 'out')
            old = dict(os.environ)
            try:
                os.environ.update({'GITHUB_OUTPUT': str(out), 'TASK': 'check how credentials are stored'})
                code = review.main(['x', 'select', *(str(files[n]) for n in ('registry.json', 'matrix.json', 'available.json', 'impl.json'))])
                self.assertEqual(code, 0)
                self.assertIn('review_blocked=true', out.read_text())
                out.write_text('')
                os.environ['TASK'] = 'focus on the transaction handling'
                review.main(['x', 'select', *(str(files[n]) for n in ('registry.json', 'matrix.json', 'available.json', 'impl.json'))])
                self.assertIn(f"model={M['glm-5.3-flash']}", out.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_review_blocked_is_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, data in (('r.json', enabled_registry()), ('m.json', SHIPPED_MATRIX),
                               ('a.json', {'data': [{'id': i} for i in ALL]}), ('i.json', [])):
                Path(tmp, name).write_text(json.dumps(data))
                paths.append(str(Path(tmp, name)))
            out = Path(tmp, 'out')
            old = dict(os.environ)
            try:
                os.environ.update({'GITHUB_OUTPUT': str(out), 'TASK': ''})
                self.assertEqual(review.main(['x', 'select', *paths]), 0)
                text = out.read_text()
                self.assertIn('review_blocked=true', text)
                self.assertIn('block_reason=', text)
            finally:
                os.environ.clear()
                os.environ.update(old)
        workflow = (ROOT / '.github/workflows/opencode.yml').read_text()
        self.assertIn('REVIEW_BLOCKED', workflow)


class GateReviewModeTests(unittest.TestCase):
    gate = load('opencode_gate')

    def test_review_command_selects_review_mode_and_implementation_is_default(self):
        split = self.gate.split_mode
        self.assertEqual(split('review'), ('review', ''))
        self.assertEqual(split('review focus on db'), ('review', 'focus on db'))
        self.assertEqual(split('reviewer rewrite x')[0], 'implement')
        self.assertEqual(split('please review this')[0], 'implement')
        self.assertEqual(split('fix the review page')[0], 'implement')

    def test_review_applies_to_pull_requests_only(self):
        with self.assertRaises(ValueError):
            self.gate.binding({'issue': {'number': 5}}, None, SHA, 'review')
        pr = {'head': {'sha': SHA, 'ref': 'feature/x'}, 'base': {'sha': BASE_C}}
        target = self.gate.binding({'issue': {'number': 5, 'pull_request': {}}}, pr, SHA, 'review')
        self.assertEqual((target['mode'], target['kind'], target['authorized_sha'], target['base_sha']),
                         ('review', 'pr', SHA, BASE_C))
        with self.assertRaises(ValueError):        # a PR without a usable base SHA cannot be reviewed
            self.gate.binding({'issue': {'number': 5, 'pull_request': {}}}, {'head': {'sha': SHA, 'ref': 'f'}, 'base': {}}, SHA, 'review')

    def test_main_authorizes_review_on_pr_and_rejects_review_on_issue(self):
        pr = {'number': 5, 'state': 'open', 'merged': False, 'base': {'sha': BASE_C, 'repo': {'full_name': 'o/r'}},
              'head': {'ref': 'feature/x', 'sha': SHA, 'repo': {'full_name': 'o/r'}}}
        def run(issue):
            with tempfile.TemporaryDirectory() as tmp:
                event = {'action': 'created', 'repository': {'full_name': 'o/r'}, 'issue': issue,
                         'comment': {'body': '/oc review check locking', 'author_association': 'OWNER',
                                     'user': {'id': 1, 'type': 'User'}}}
                Path(tmp, 'e.json').write_text(json.dumps(event))
                Path(tmp, 'pr.json').write_text(json.dumps(pr))
                out = Path(tmp, 'out')
                old = dict(os.environ)
                try:
                    os.environ.update({'GITHUB_EVENT_PATH': str(Path(tmp, 'e.json')), 'GITHUB_OUTPUT': str(out),
                                       'GITHUB_EVENT_NAME': 'issue_comment', 'PR_JSON_PATH': str(Path(tmp, 'pr.json')),
                                       'GITHUB_REPOSITORY': 'o/r', 'ALLOWED_ACTOR_IDS': '1', 'GITHUB_SHA': 'd' * 40})
                    self.gate.main()
                    return out.read_text()
                finally:
                    os.environ.clear()
                    os.environ.update(old)
        text = run({'number': 5, 'pull_request': {}})
        for expected in ('authorized=true', 'mode=review', 'kind=pr', 'task<<', f'base_sha={BASE_C}', f'authorized_sha={SHA}'):
            self.assertIn(expected, text)
        self.assertIn('check locking', text)
        self.assertNotIn('review check', text)
        self.assertIn('authorized=false', run({'number': 5}))


if __name__ == '__main__':
    unittest.main()
