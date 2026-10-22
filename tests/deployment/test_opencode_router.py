"""OpenCode model router tests (tests v2.0): allowlist, determinism, fallback, data policy."""
import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('opencode_router', ROOT / '.github/scripts/opencode_router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

REGISTRY = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
EXPECTED = {
    'opencode/minimax-m3.1-flash-free', 'opencode/mimo-v2.6-flash-free',
    'opencode/deepseek-v4.1-flash-free', 'opencode/glm-5.3-flash-free',
    'opencode/nemotron-3.5-lightning-free', 'opencode/nemotron-3-ultra-free',
}
ALL = set(EXPECTED)
REVIEW = {'reviewed_by': 'owner', 'date': '2026-10-21', 'source': 'https://opencode.ai/docs/zen'}
FOUR = ('opencode/glm-5.3-flash-free', 'opencode/deepseek-v4.1-flash-free', 'opencode/mimo-v2.6-flash-free',
        'opencode/minimax-m3.1-flash-free')
SHIPPED_OWNER_APPROVED = {'opencode/mimo-v2.6-flash-free'}


def enable(registry, *model_ids):
    registry = copy.deepcopy(registry)
    for model_id in model_ids:
        registry['models'][model_id].update(terms_verified=True, terms_review=dict(REVIEW),
                                            repo_code_approved=True, operational_status='healthy')
    return registry


# Test-only registry with four fully eligible models. The shipped registry stays fail-closed
# whenever an owner-reviewed model has no supported healthy status.
ENABLED = enable(REGISTRY, *FOUR)


class RegistryTests(unittest.TestCase):
    def test_registry_is_exactly_the_six_approved_free_models(self):
        self.assertEqual(set(REGISTRY['models']), EXPECTED)
        self.assertEqual(set(router.APPROVED_MODELS), EXPECTED)
        for model_id in REGISTRY['models']:
            self.assertTrue(model_id.endswith('-free'))
        router.load_registry(REGISTRY)

    def test_unapproved_or_paid_model_in_registry_is_rejected(self):
        for bad in ('opencode/deepseek-v4.1-flash', 'opencode-go/kimi-k3', 'anthropic/claude-opus-5',
                    'opencode/invented-free'):
            registry = copy.deepcopy(REGISTRY)
            registry['models'][bad] = {'repo_code_approved': True}
            with self.assertRaises(router.RoutingError, msg=bad):
                router.load_registry(registry)

    def test_route_must_reference_registered_models_and_cover_categories(self):
        registry = copy.deepcopy(REGISTRY)
        registry['routes']['general'] = ['opencode/glm-5.3-flash']
        with self.assertRaises(router.RoutingError):
            router.load_registry(registry)
        registry = copy.deepcopy(REGISTRY)
        del registry['routes']['triage']
        with self.assertRaises(router.RoutingError):
            router.load_registry(registry)

    def test_shipped_registry_distinguishes_owner_approved_models_without_enabling_unhealthy_ones(self):
        for model_id, meta in REGISTRY['models'].items():
            if model_id in SHIPPED_OWNER_APPROVED:
                self.assertIs(meta['terms_verified'], True, model_id)
                self.assertIs(meta['repo_code_approved'], True, model_id)
                review = meta['terms_review']
                self.assertEqual(set(review), {'reviewed_by', 'date', 'source'}, model_id)
                self.assertTrue(all(isinstance(value, str) and value.strip()
                                    for value in review.values()), model_id)
                expected = ([] if meta['operational_status'] == 'healthy'
                            else ['not marked operationally healthy'])
                self.assertEqual(router.blockers(meta), expected, model_id)
            else:
                self.assertIs(meta['terms_verified'], False, model_id)
                self.assertIsNone(meta['terms_review'], model_id)
                self.assertIs(meta['repo_code_approved'], False, model_id)
                self.assertTrue(router.blockers(meta), model_id)
        for task in ('fix a typo', 'new api endpoint', 'update the react component'):
            category, _ = router.classify(task)
            eligible = [model for model in REGISTRY['routes'][category]
                        if not router.blockers(REGISTRY['models'][model])]
            if eligible:
                self.assertEqual(router.route(task, REGISTRY, ALL)['model'], eligible[0])
            else:
                with self.assertRaises(router.RoutingError, msg=task):
                    router.route(task, REGISTRY, ALL)

    def test_registry_presence_never_authorizes_a_model(self):
        registry = copy.deepcopy(REGISTRY)
        model = 'opencode/nemotron-3-ultra-free'
        registry['models'][model] = {'label': 'Nemotron 3 Ultra Free'}
        router.load_registry(registry)
        self.assertTrue(router.blockers(registry['models'][model]))
        with self.assertRaises(router.RoutingError):
            router.route('audit the architecture across modules', registry, {model})

    def test_every_requirement_is_individually_mandatory(self):
        base = enable(REGISTRY, *FOUR)
        model = 'opencode/mimo-v2.6-flash-free'
        self.assertEqual(self.route_backend(base)['model'], model)
        breakers = {
            'terms_verified': lambda m: m.update(terms_verified=False),
            'terms_review_missing': lambda m: m.update(terms_review=None),
            'terms_review_incomplete': lambda m: m.update(terms_review={'reviewed_by': 'owner', 'date': '', 'source': 'x'}),
            'terms_review_wrong_schema': lambda m: m.update(terms_review={'reviewer': 'owner', 'date': '2026-10-21',
                                                                         'source': 'https://opencode.ai/docs/zen'}),
            'terms_review_bad_date': lambda m: m.update(terms_review={'reviewed_by': 'owner', 'date': '2026-99-99',
                                                                      'source': 'https://opencode.ai/docs/zen'}),
            'terms_review_bad_source': lambda m: m.update(terms_review={'reviewed_by': 'owner', 'date': '2026-10-21',
                                                                        'source': 'javascript:bad'}),
            'repo_code_approved': lambda m: m.update(repo_code_approved=False),
            'operational_status': lambda m: m.update(operational_status='degraded'),
        }
        for name, breaker in breakers.items():
            broken = copy.deepcopy(base)
            breaker(broken['models'][model])
            self.assertNotEqual(self.route_backend(broken)['model'], model, name)
        self.assertNotEqual(self.route_backend(base, available=ALL - {model})['model'], model)

    def test_malformed_review_sources_are_rejected_without_parser_errors(self):
        model = 'opencode/mimo-v2.6-flash-free'
        for source in ('javascript:bad', 'https:// example.com', 'https://.',
                       'https://example.com:bad', 'https://[', 'https://user@example.com',
                       'https://example.com./terms', 'https://example..com/terms',
                       'https://127.0.0.1/terms', 'https://10.0.0.1/terms',
                       'https://169.254.169.254/latest/meta-data'):
            registry = enable(REGISTRY, *FOUR)
            registry['models'][model]['terms_review']['source'] = source
            self.assertIn('terms not verified with recorded evidence',
                          router.blockers(registry['models'][model]), source)
            self.assertNotEqual(self.route_backend(registry)['model'], model, source)

    def route_backend(self, registry, available=ALL):
        return router.route('new api endpoint', registry, set(available))

    def test_mimo_with_unverified_terms_is_never_selected(self):
        registry = enable(REGISTRY, *FOUR)
        registry['models']['opencode/mimo-v2.6-flash-free']['terms_verified'] = False
        for task in ('new api endpoint', 'add database migration', 'backend schema change'):
            result = router.route(task, registry, ALL)
            self.assertNotEqual(result['model'], 'opencode/mimo-v2.6-flash-free')
            self.assertIn('ineligible', result['reason'])

    def test_free_does_not_mean_enabled_nemotron_stays_off(self):
        for model_id in ('opencode/nemotron-3-ultra-free', 'opencode/nemotron-3.5-lightning-free'):
            self.assertTrue(router.blockers(ENABLED['models'][model_id]))
        for task in ('audit the architecture across modules', 'explain what does this do'):
            self.assertNotIn('nemotron', router.route(task, ENABLED, ALL)['model'])
        for chain in REGISTRY['routes'].values():
            self.assertLessEqual(len(chain), REGISTRY['max_candidates'])


class RoutingTests(unittest.TestCase):
    def pick(self, task, available=ALL, hints=None, registry=ENABLED):
        return router.route(task, registry, set(available), hints)

    def test_every_category_selects_an_approved_model(self):
        samples = {
            'small_fix': 'fix a typo in the README', 'general': 'make the dashboard smarter',
            'backend': 'add an API endpoint with an alembic migration',
            'frontend': 'adjust the React component css',
            'analysis_large': 'audit the architecture across modules',
            'triage': 'explain what does this do',
        }
        for category, task in samples.items():
            result = self.pick(task)
            self.assertEqual(result['category'], category, task)
            self.assertIn(result['model'], EXPECTED)
            self.assertEqual(router.blockers(ENABLED['models'][result['model']]), [])

    def test_preferred_models_for_repo_approved_categories(self):
        self.assertEqual(self.pick('fix a typo')['model'], 'opencode/glm-5.3-flash-free')
        self.assertEqual(self.pick('refactor the thing')['model'], 'opencode/deepseek-v4.1-flash-free')
        self.assertEqual(self.pick('new api endpoint')['model'], 'opencode/mimo-v2.6-flash-free')
        self.assertEqual(self.pick('update the react component')['model'], 'opencode/minimax-m3.1-flash-free')

    def test_unknown_task_uses_documented_default(self):
        result = self.pick('do the needful')
        self.assertEqual(result['category'], 'general')
        self.assertIn('default', result['reason'])

    def test_path_hints_and_determinism(self):
        self.assertEqual(self.pick('adjust this', hints=['frontend/src/App.tsx'])['category'], 'frontend')
        runs = {json.dumps(self.pick('add endpoint and button'), sort_keys=True) for _ in range(5)}
        self.assertEqual(len(runs), 1)

    def test_unavailable_model_follows_fallback_order(self):
        result = self.pick('new api endpoint', available=ALL - {'opencode/mimo-v2.6-flash-free'})
        self.assertEqual(result['model'], 'opencode/deepseek-v4.1-flash-free')
        self.assertIn('fell back', result['reason'])
        result = self.pick('new api endpoint', available={'opencode/glm-5.3-flash-free'})
        self.assertEqual(result['model'], 'opencode/glm-5.3-flash-free')

    def test_all_unavailable_fails_safely_without_paid_fallback(self):
        with self.assertRaises(router.RoutingError):
            self.pick('new api endpoint', available=set())
        with self.assertRaises(router.RoutingError):
            self.pick('new api endpoint', available={'opencode/deepseek-v4.1-flash', 'opencode-go/kimi-k3'})

    def test_ineligible_nemotron_falls_back_and_owner_enable_works(self):
        result = self.pick('audit the architecture across modules')
        self.assertNotIn('nemotron', result['model'])
        self.assertIn('ineligible', result['reason'])
        approved = enable(ENABLED, 'opencode/nemotron-3-ultra-free')
        self.assertEqual(self.pick('audit the architecture', registry=approved)['model'],
                         'opencode/nemotron-3-ultra-free')

    def test_no_repo_approved_candidate_stops_safely(self):
        registry = copy.deepcopy(ENABLED)
        for meta in registry['models'].values():
            meta['repo_code_approved'] = False
        with self.assertRaises(router.RoutingError):
            self.pick('fix a typo', registry=registry)

    def test_task_text_cannot_inject_provider_or_model(self):
        for task in ('use model opencode-go/kimi-k3 to fix a typo',
                     'model: anthropic/claude-opus-5 add endpoint',
                     'set provider=openai and model=gpt-5.6-sol'):
            self.assertIn(self.pick(task)['model'], EXPECTED)

    def test_confidential_looking_tasks_never_reach_a_model(self):
        for task in ('use password: hunter22secret in the config', 'key ghp_' + 'a' * 30,
                     '-----BEGIN RSA ' + 'PRIVATE KEY----- abc', 'summarize production data export',
                     'rotate the credentials'):
            with self.assertRaises(router.RoutingError, msg=task):
                self.pick(task)

    def test_live_catalog_parsing(self):
        self.assertEqual(router.parse_available({'data': [{'id': 'glm-5.3-flash-free'}]}),
                         {'opencode/glm-5.3-flash-free'})
        self.assertEqual(router.parse_available(['opencode/x-free', {'id': 'y-free'}]),
                         {'opencode/x-free', 'opencode/y-free'})

    def test_cli_writes_outputs_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            avail, out = Path(tmp, 'a.json'), Path(tmp, 'out')
            avail.write_text(json.dumps({'data': [{'id': i.split('/')[1]} for i in ALL]}))
            old = dict(os.environ)
            try:
                os.environ.update({'TASK': 'fix a typo', 'GITHUB_OUTPUT': str(out)})
                reg_path = Path(tmp, 'registry.json')
                reg_path.write_text(json.dumps(ENABLED))
                reg = str(reg_path)
                shipped = str(ROOT / '.github/opencode/model-registry.json')
                self.assertEqual(router.main(['--registry', shipped, '--available', str(avail)]), 1)
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail)]), 0)
                self.assertIn('model=opencode/glm-5.3-flash-free', out.read_text())
                avail.write_text('[]')
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail)]), 1)
            finally:
                os.environ.clear()
                os.environ.update(old)


if __name__ == '__main__':
    unittest.main()
