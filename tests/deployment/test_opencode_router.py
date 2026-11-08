"""OpenCode model router tests (tests v2.0): allowlist, determinism, fallback, data policy."""
import copy
import importlib.util
import json
import os
import tempfile
from unittest import mock
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('opencode_router', ROOT / '.github/scripts/opencode_router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)
import sys
sys.path.insert(0, str(ROOT / '.github/scripts'))
import opencode_proxy as proxy  # noqa: E402  (shares the router module through sys.path)

SHIPPED = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
# Most tests below prove fail-closed routing from a registry where no model has a recorded health status, so they
# use the shipped records with the one healthy model reset. test_shipped_registry_* pins what the file really says.
REGISTRY = copy.deepcopy(SHIPPED)
REGISTRY['models']['opencode-go/glm-5.2']['operational_status'] = 'unknown'
GO = 'opencode-go/qwen3.8-max'
ALL_GO = set(router.GO_MODELS)
EXPECTED = ALL_GO
ALL = set(router.GO_MODELS)
REVIEW = {'reviewed_by': 'owner', 'date': '2026-10-21', 'source': 'https://opencode.ai/docs/zen'}
FOUR = ('opencode-go/glm-5.3-flash', 'opencode-go/glm-5.2', 'opencode-go/kimi-k2.7-code',
        'opencode-go/kimi-k3.1-pro')
# Passed the owner-run production probe on 2026-11-05 (PR 219, run 41951295355).
SHIPPED_OWNER_ENABLED = {'opencode-go/glm-5.2'}
# Go models whose terms, data-policy acknowledgement and repository-code approval the owner recorded on 2026-11-04.
SHIPPED_GO_APPROVED_UNHEALTHY = {'opencode-go/glm-5.3-flash', 'opencode-go/kimi-k2.7-code', 'opencode-go/kimi-k3.1-pro'}
# One task per routing category, used to prove shipped eligibility and fail-closed routing.
SHIPPED_TASKS = {
    'small_fix': 'fix a typo in the README',
    'general': 'make the dashboard smarter',
    'backend': 'add an API endpoint with an alembic migration',
    'frontend': 'adjust the React component css',
    'analysis_large': 'audit the architecture across modules',
    'triage': 'explain what does this do',
}


ACK = {'go_data_policy': {'acknowledged_by': 'owner', 'date': '2026-10-21', 'term': router.DATA_USE_TERM}}


def ack_for(model_id):
    return copy.deepcopy(ACK)


def unacknowledge(registry, *model_ids):
    registry = copy.deepcopy(registry)
    for model_id in model_ids:
        registry['models'][model_id]['owner_acknowledgements'] = {}
    return registry


def acknowledge(registry, *model_ids):
    registry = copy.deepcopy(registry)
    for model_id in model_ids:
        registry['models'][model_id]['owner_acknowledgements'] = ack_for(model_id)
    return registry


def enable(registry, *model_ids):
    registry = copy.deepcopy(registry)
    for model_id in model_ids:
        registry['models'][model_id].update(terms_verified=True, terms_review=dict(REVIEW),
                                            repo_code_approved=True, operational_status='healthy',
                                            owner_acknowledgements=ack_for(model_id))
    return registry


# Test-only registry with four fully eligible models. The shipped registry stays fail-closed
# whenever an owner-reviewed model has no supported healthy status.
ENABLED = enable(REGISTRY, *FOUR)


class RegistryTests(unittest.TestCase):
    def test_registry_lists_exactly_the_approved_go_models_and_no_free_model(self):
        self.assertEqual(set(REGISTRY['models']), ALL_GO)
        self.assertEqual(set(router.APPROVED_MODELS), ALL_GO)
        self.assertEqual(len(ALL_GO), 16)
        self.assertIn(GO, ALL_GO)
        for model_id in REGISTRY['models']:
            self.assertTrue(model_id.startswith('opencode-go/'), model_id)
            self.assertFalse(model_id.endswith('-free'), model_id)
        router.load_registry(REGISTRY)

    def test_an_enabled_go_model_is_chosen_only_in_categories_whose_chain_lists_it(self):
        model = 'opencode-go/kimi-k2.7-code'
        registry = enable(REGISTRY, model)
        self.assertEqual([m for m, meta in registry['models'].items() if not router.blockers(meta)], [model])
        for category, task in SHIPPED_TASKS.items():
            with self.subTest(category=category):
                if model in registry['routes'][category]:
                    result = router.route(task, registry, ALL)
                    self.assertEqual((result['category'], result['model']), (category, model))
                else:
                    with self.assertRaises(router.RoutingError):
                        router.route(task, registry, ALL)

    # ---- owner-only text-only probe: waives the health requirement and nothing else.
    def test_probe_waives_only_the_health_requirement(self):
        meta = REGISTRY['models']['opencode-go/glm-5.2']
        self.assertEqual(meta['operational_status'], 'unknown')
        self.assertEqual(router.blockers(meta), ['not marked operationally healthy'])
        self.assertEqual(router.blockers(meta, probe=True), [])
        # Without the owner's acknowledgement even the probe stays blocked.
        bare = dict(meta, owner_acknowledgements={})
        self.assertEqual(router.blockers(bare), ['owner has not acknowledged the data-use term',
                                                 'not marked operationally healthy'])
        self.assertEqual(router.blockers(bare, probe=True), ['owner has not acknowledged the data-use term'])
        for field, broken in (('terms_verified', False), ('terms_review', None), ('repo_code_approved', False),
                              ('owner_acknowledgements', {})):
            with self.subTest(field=field):
                changed = dict(meta, **{field: broken})
                self.assertTrue(router.blockers(changed, probe=True))
        # A model with no recorded terms or approval stays blocked even for the probe.
        unreviewed = REGISTRY['models'][GO]
        self.assertTrue(router.blockers(unreviewed, probe=True))

    def test_probe_selects_an_unhealthy_reviewed_model_but_normal_routing_does_not(self):
        mimo = 'opencode-go/glm-5.2'
        with self.assertRaises(router.RoutingError):
            router.route('Reply exactly PROBE_OK', REGISTRY, ALL)
        result = router.route('Reply exactly PROBE_OK', REGISTRY, ALL, probe=True)
        self.assertEqual(result['model'], mimo)
        with self.assertRaises(router.RoutingError):      # still needs the live catalog
            router.route('Reply exactly PROBE_OK', REGISTRY, set(), probe=True)
        with self.assertRaises(router.RoutingError):      # the probe never waives the acknowledgement
            router.route('Reply exactly PROBE_OK', unacknowledge(REGISTRY, mimo), ALL, probe=True)

    def test_probe_cli_flag_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            avail = Path(tmp, 'a.json')
            avail.write_text(json.dumps({'data': [{'id': i.split('/')[1]} for i in ALL]}))
            reg = Path(tmp, 'baseline.json')
            reg.write_text(json.dumps(REGISTRY))
            reg = str(reg)
            bare_reg = Path(tmp, 'r.json')
            bare_reg.write_text(json.dumps(unacknowledge(REGISTRY, 'opencode-go/glm-5.2')))
            old = dict(os.environ)
            try:
                os.environ['TASK'] = 'Reply exactly PROBE_OK'
                os.environ.pop('GITHUB_OUTPUT', None)
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail)]), 1)
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail), '--probe']), 0)
                self.assertEqual(router.main(['--registry', str(bare_reg), '--available', str(avail), '--probe']), 1)
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_proxy_binds_an_unhealthy_reviewed_model_only_in_probe_mode(self):
        mimo = 'opencode-go/glm-5.2'
        with self.assertRaises(proxy.PolicyError) as caught:
            proxy.validate_bound_model(mimo, REGISTRY, set())
        self.assertEqual(caught.exception.code, 'model_disabled_or_unhealthy')
        self.assertEqual(proxy.validate_bound_model(mimo, REGISTRY, set(), probe=True), 'glm-5.2')
        with self.assertRaises(proxy.PolicyError):        # no acknowledgement recorded
            proxy.validate_bound_model(mimo, unacknowledge(REGISTRY, mimo), set(), probe=True)
        unreviewed = copy.deepcopy(REGISTRY)
        unreviewed['models'][mimo]['repo_code_approved'] = False
        with self.assertRaises(proxy.PolicyError):
            proxy.validate_bound_model(mimo, unreviewed, set(), probe=True)

    def test_proxy_cli_accepts_probe_flag_only_when_given(self):
        mimo = 'opencode-go/glm-5.2'
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'registry.json')
            registry.write_text(json.dumps(REGISTRY))
            unacknowledged = Path(tmp, 'unacknowledged.json')
            unacknowledged.write_text(json.dumps(unacknowledge(REGISTRY, mimo)))
            argv = ['opencode_proxy.py', str(Path(tmp, 'port')), mimo, str(registry)]
            started = []

            class Server:
                def serve_forever(self):
                    started.append(True)

            old = dict(os.environ)
            try:
                os.environ['OPENCODE_API_KEY'] = 'k'
                with mock.patch.object(proxy, 'serve', return_value=(Server(), None)):
                    self.assertEqual(proxy.main(argv), 1)
                    self.assertEqual(started, [])
                    os.environ['OPENCODE_API_KEY'] = 'k'
                    self.assertEqual(proxy.main(argv + ['--probe']), 0)
                    self.assertEqual(started, [True])
                    os.environ['OPENCODE_API_KEY'] = 'k'
                    refused = argv[:3] + [str(unacknowledged), '--probe']
                    self.assertEqual(proxy.main(refused), 1)   # the probe never waives the acknowledgement
                    self.assertEqual(started, [True])
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_unapproved_or_paid_model_in_registry_is_rejected(self):
        for bad in ('opencode/deepseek-v4.1-flash', 'opencode-go/kimi-k4', 'opencode-go/muse-spark-1.3-contributor', 'anthropic/claude-opus-5',
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

    def test_shipped_registry_keeps_owner_approved_unhealthy_models_disabled(self):
        # The owner-approved GLM record is eligible in the shipped registry. MiMo has terms
        # evidence and repository-code approval but remains blocked until health is recorded.
        for model_id, meta in SHIPPED['models'].items():
            if model_id in SHIPPED_GO_APPROVED_UNHEALTHY:
                self.assertIs(meta['terms_verified'], True, model_id)
                self.assertIs(meta['repo_code_approved'], True, model_id)
                self.assertEqual(meta['terms_review'], {
                    'reviewed_by': 'AhmedMahmoud2222', 'date': '2026-11-04',
                    'source': 'https://opencode.ai/v2/docs/console/go/'}, model_id)
                self.assertEqual(meta['owner_acknowledgements'], {'go_data_policy': {
                    'acknowledged_by': 'AhmedMahmoud2222', 'date': '2026-11-04',
                    'term': router.DATA_USE_TERM}}, model_id)
                self.assertNotEqual(meta['operational_status'], 'healthy', model_id)
                self.assertEqual(router.blockers(meta),
                                 ['not marked operationally healthy'], model_id)
            elif model_id in SHIPPED_OWNER_ENABLED:
                self.assertIs(meta['terms_verified'], True, model_id)
                self.assertIs(meta['repo_code_approved'], True, model_id)
                self.assertEqual(meta['operational_status'], 'healthy', model_id)
                self.assertEqual(router.blockers(meta), [], model_id)
            else:
                self.assertIs(meta['terms_verified'], False, model_id)
                self.assertIsNone(meta['terms_review'], model_id)
                self.assertIs(meta['repo_code_approved'], False, model_id)
                self.assertTrue(router.blockers(meta), model_id)
        self.assertEqual(
            [model_id for model_id, meta in SHIPPED['models'].items()
             if not router.blockers(meta)],
            sorted(SHIPPED_OWNER_ENABLED), 'only the owner-enabled GLM model is eligible')
        self.assertEqual(set(SHIPPED_TASKS), set(router.CATEGORIES))
        for category, task in SHIPPED_TASKS.items():
            self.assertEqual(router.classify(task)[0], category, task)
            eligible = [model for model in REGISTRY['routes'][category]
                        if not router.blockers(REGISTRY['models'][model])]
            if eligible:
                self.assertEqual(eligible, ['opencode-go/glm-5.3-flash'], category)
                self.assertEqual(router.route(task, REGISTRY, ALL)['model'], eligible[0], task)
            else:
                with self.assertRaises(router.RoutingError, msg=task):
                    router.route(task, REGISTRY, ALL)

    def test_registry_presence_never_authorizes_a_model(self):
        registry = copy.deepcopy(REGISTRY)
        model = 'opencode-go/longcat-2.0'
        registry['models'][model] = {'label': 'Nemotron 3 Ultra Free'}
        router.load_registry(registry)
        self.assertTrue(router.blockers(registry['models'][model]))
        with self.assertRaises(router.RoutingError):
            router.route('audit the architecture across modules', registry, {model})

    def test_every_requirement_is_individually_mandatory(self):
        base = enable(REGISTRY, *FOUR)
        model = 'opencode-go/kimi-k2.7-code'
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
            'acknowledgement_missing': lambda m: m.update(owner_acknowledgements={}),
            'acknowledgement_reworded_term': lambda m: m['owner_acknowledgements']['go_data_policy'].update(term='old term'),
            'acknowledgement_blank_owner': lambda m: m['owner_acknowledgements']['go_data_policy'].update(acknowledged_by=' '),
            'acknowledgement_bad_date': lambda m: m['owner_acknowledgements']['go_data_policy'].update(date='2026-99-99'),
            'acknowledgement_extra_key': lambda m: m['owner_acknowledgements']['go_data_policy'].update(extra='x'),
            'operational_status': lambda m: m.update(operational_status='degraded'),
        }
        for name, breaker in breakers.items():
            broken = copy.deepcopy(base)
            breaker(broken['models'][model])
            self.assertNotEqual(self.route_backend(broken)['model'], model, name)
        self.assertNotEqual(self.route_backend(base, available=ALL - {model})['model'], model)

    def test_malformed_review_sources_are_rejected_without_parser_errors(self):
        model = 'opencode-go/kimi-k2.7-code'
        for source in ('javascript:bad', 'https:// example.com', 'https://.',
                       'https://example.com:bad', 'https://[', 'https://user@example.com',
                       'https://example.com./terms', 'https://example..com/terms',
                       'https://127.0.0.1/terms', 'https://10.0.0.1/terms',
                       'https://169.254.169.254/latest/meta-data',
                       'https://127.000.000.001/terms', 'https://0177.0.0.1/terms',
                       'https://0x7f.0.0.1/terms'):
            registry = enable(REGISTRY, *FOUR)
            registry['models'][model]['terms_review']['source'] = source
            self.assertIn('terms not verified with recorded evidence',
                          router.blockers(registry['models'][model]), source)
            self.assertNotEqual(self.route_backend(registry)['model'], model, source)

    def route_backend(self, registry, available=ALL):
        return router.route('new api endpoint', registry, set(available))

    def test_a_model_with_unverified_terms_is_never_selected(self):
        registry = enable(REGISTRY, *FOUR)
        registry['models']['opencode-go/kimi-k2.7-code']['terms_verified'] = False
        for task in ('new api endpoint', 'add database migration', 'backend schema change'):
            result = router.route(task, registry, ALL)
            self.assertNotEqual(result['model'], 'opencode-go/kimi-k2.7-code')
            self.assertIn('ineligible', result['reason'])

    def test_registry_presence_does_not_enable_the_models_the_owner_has_not_approved(self):
        for model_id in ALL_GO - set(FOUR):
            self.assertTrue(router.blockers(ENABLED['models'][model_id]), model_id)
        for task in ('audit the architecture across modules', 'explain what does this do'):
            self.assertIn(router.route(task, ENABLED, ALL)['model'], FOUR)
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
            self.assertIn(result['model'], FOUR)
            self.assertEqual(router.blockers(ENABLED['models'][result['model']]), [])

    def test_preferred_models_for_repo_approved_categories(self):
        self.assertEqual(self.pick('fix a typo')['model'], 'opencode-go/glm-5.3-flash')
        self.assertEqual(self.pick('refactor the thing')['model'], 'opencode-go/glm-5.2')
        self.assertEqual(self.pick('new api endpoint')['model'], 'opencode-go/kimi-k2.7-code')
        self.assertEqual(self.pick('update the react component')['model'], 'opencode-go/kimi-k2.7-code')
        self.assertEqual(self.pick('audit the architecture across modules')['model'], 'opencode-go/kimi-k3.1-pro')

    def test_unknown_task_uses_documented_default(self):
        result = self.pick('do the needful')
        self.assertEqual(result['category'], 'general')
        self.assertIn('default', result['reason'])

    def test_path_hints_and_determinism(self):
        self.assertEqual(self.pick('adjust this', hints=['frontend/src/App.tsx'])['category'], 'frontend')
        runs = {json.dumps(self.pick('add endpoint and button'), sort_keys=True) for _ in range(5)}
        self.assertEqual(len(runs), 1)

    def test_unavailable_model_follows_fallback_order(self):
        result = self.pick('new api endpoint', available=ALL - {'opencode-go/kimi-k2.7-code'})
        self.assertEqual(result['model'], 'opencode-go/glm-5.2')
        self.assertIn('fell back', result['reason'])
        result = self.pick('new api endpoint', available={'opencode-go/glm-5.2'})
        self.assertEqual(result['model'], 'opencode-go/glm-5.2')

    def test_all_unavailable_fails_safely_without_paid_fallback(self):
        with self.assertRaises(router.RoutingError):
            self.pick('new api endpoint', available=set())
        with self.assertRaises(router.RoutingError):
            self.pick('new api endpoint', available={'opencode/deepseek-v4.1-flash', 'opencode-go/kimi-k3', 'opencode/glm-5.3-flash-free'})

    def test_an_unapproved_model_is_skipped_and_owner_approval_enables_it(self):
        result = self.pick('new api endpoint', registry=enable(REGISTRY, 'opencode-go/mimo-v2.6-pro'))
        self.assertEqual(result['model'], 'opencode-go/mimo-v2.6-pro')
        self.assertIn('ineligible', result['reason'])
        self.assertNotEqual(self.pick('new api endpoint')['model'], 'opencode-go/mimo-v2.6-pro')

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
            self.assertIn(self.pick(task)['model'], FOUR)

    def test_confidential_looking_tasks_never_reach_a_model(self):
        for task in ('use password: hunter22secret in the config', 'key ghp_' + 'a' * 30,
                     '-----BEGIN RSA ' + 'PRIVATE KEY----- abc', 'summarize production data export',
                     'rotate the credentials'):
            with self.assertRaises(router.RoutingError, msg=task):
                self.pick(task)

    def test_live_catalog_parsing(self):
        self.assertEqual(router.parse_available({'data': [{'id': 'glm-5.3-flash'}]}),
                         {'opencode-go/glm-5.3-flash'})
        self.assertEqual(router.parse_available(['opencode-go/x', {'id': 'y'}]),
                         {'opencode-go/x', 'opencode-go/y'})

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
                ineligible = copy.deepcopy(REGISTRY)
                ineligible['models']['opencode-go/glm-5.3-flash']['terms_verified'] = False
                ineligible_path = Path(tmp, 'ineligible-registry.json')
                ineligible_path.write_text(json.dumps(ineligible))
                self.assertEqual(router.main(['--registry', str(ineligible_path), '--available', str(avail)]), 1)
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail)]), 0)
                self.assertIn('model=opencode-go/glm-5.3-flash', out.read_text())
                avail.write_text('[]')
                self.assertEqual(router.main(['--registry', reg, '--available', str(avail)]), 1)
            finally:
                os.environ.clear()
                os.environ.update(old)


class GoRotationTests(unittest.TestCase):
    def test_every_go_model_serves_at_least_one_category_and_no_category_depends_on_one_model(self):
        used = {m for chain in REGISTRY['routes'].values() for m in chain if m in ALL_GO}
        self.assertEqual(used, ALL_GO)
        for category, chain in REGISTRY['routes'].items():
            self.assertGreaterEqual(len([m for m in chain if m in ALL_GO]), 3, category)

    def test_each_category_rotates_to_the_next_go_model_when_the_first_is_unavailable(self):
        registry = enable(REGISTRY, *ALL_GO)
        tasks = {'small_fix': 'fix a typo in the README', 'backend': 'add an API endpoint with an alembic migration',
                 'frontend': 'adjust the React component css', 'analysis_large': 'audit the architecture across modules',
                 'triage': 'explain what does this do', 'general': 'make the dashboard smarter'}
        for category, task in tasks.items():
            go_chain = [m for m in registry['routes'][category] if m in ALL_GO]
            self.assertEqual(router.route(task, registry, set(go_chain))['model'], go_chain[0], category)
            self.assertEqual(router.route(task, registry, set(go_chain[1:]))['model'], go_chain[1], category)

    def test_no_go_model_trains_on_data_or_needs_a_non_chat_endpoint(self):
        for banned in ('iceman', 'muse-spark', 'beam-free', 'gpt-', 'grok', 'claude-', 'gemini', 'deepseek'):
            self.assertFalse([m for m in ALL_GO if banned in m], banned)


class GoGateTests(unittest.TestCase):
    """OpenCode Go only: the acknowledgement is exact, normalization stays inside the Go namespace, and the
    proxy reaches only the Go endpoints."""
    BODY = json.dumps({'model': 'qwen3.8-max', 'messages': [{'role': 'user', 'content': 'x'}]}).encode()
    JSON = {'Content-Type': 'application/json'}

    def test_unapproved_go_entries_are_fully_blocked_and_chains_hold_only_go_models(self):
        for model_id in ALL_GO - SHIPPED_GO_APPROVED_UNHEALTHY - SHIPPED_OWNER_ENABLED:
            meta = REGISTRY['models'][model_id]
            self.assertIs(meta['terms_verified'], False, model_id)
            self.assertIs(meta['repo_code_approved'], False, model_id)
            self.assertEqual(meta['owner_acknowledgements'], {}, model_id)
            self.assertEqual(len(router.blockers(meta)), 4, model_id)
        for chain in REGISTRY['routes'].values():
            self.assertEqual(len(chain), len(set(chain)))
            self.assertTrue(all(m in ALL_GO for m in chain))
            self.assertLessEqual(len(chain), REGISTRY['max_candidates'])

    def test_the_acknowledgement_must_carry_the_exact_go_term(self):
        registry = enable(REGISTRY, GO)
        self.assertEqual(router.blockers(registry['models'][GO]), [])
        old_term = copy.deepcopy(registry)
        old_term['models'][GO]['owner_acknowledgements'] = {'free_period_data_use': {
            'acknowledged_by': 'owner', 'date': '2026-10-21',
            'term': 'During its free period, collected data may be used to improve the model'}}
        self.assertIn('owner has not acknowledged the data-use term', router.blockers(old_term['models'][GO]))
        reworded = copy.deepcopy(registry)
        reworded['models'][GO]['owner_acknowledgements']['go_data_policy']['term'] = 'ok'
        self.assertTrue(router.blockers(reworded['models'][GO]))

    def test_an_enabled_go_model_is_selected_only_when_it_is_in_the_live_catalog(self):
        registry = enable(REGISTRY, GO)
        self.assertEqual(router.route('new api endpoint', registry, {GO})['model'], GO)
        with self.assertRaises(router.RoutingError):
            router.route('new api endpoint', registry, set(ALL) - {GO})

    def test_ids_normalize_into_the_go_namespace_and_free_or_unknown_ids_are_rejected(self):
        self.assertEqual(router.normalize('qwen3.8-max'), GO)
        self.assertEqual(router.normalize(GO), GO)
        for bad in ('opencode-go/kimi-k4', 'opencode/glm-5.3-flash-free', 'opencode/qwen3.8-max'):
            registry = copy.deepcopy(REGISTRY)
            registry['models'][bad] = copy.deepcopy(registry['models'][GO])
            with self.assertRaises(router.RoutingError, msg=bad):
                router.load_registry(registry)

    def test_proxy_binds_go_models_with_go_paths_only(self):
        registry = enable(REGISTRY, GO)
        self.assertEqual(proxy.validate_bound_model(GO, registry, set()), 'qwen3.8-max')
        with self.assertRaises(proxy.PolicyError):
            proxy.validate_bound_model(GO, REGISTRY, set())
        with self.assertRaises(proxy.PolicyError):
            proxy.validate_bound_model(GO, unacknowledge(registry, GO), set())
        with self.assertRaises(proxy.PolicyError) as caught:
            proxy.validate_bound_model('opencode/glm-5.3-flash-free', registry, set())
        self.assertEqual(caught.exception.code, 'model_not_approved')
        proxy.check_request('POST', '/inference/go/openai/v1/chat/completions', self.JSON, self.BODY, 'qwen3.8-max')
        proxy.check_request('GET', '/inference/go/v1/models', {}, None, 'qwen3.8-max')
        for method, path in (('POST', '/inference/openai/v1/chat/completions'), ('GET', '/zen/v1/models'),
                             ('GET', '/inference/openai/v1/models'), ('POST', '/inference/go/v1/chat/completions')):
            with self.assertRaises(proxy.PolicyError, msg=path) as caught:
                proxy.check_request(method, path, self.JSON, self.BODY if method == 'POST' else None, 'qwen3.8-max')
            self.assertEqual(caught.exception.code, 'endpoint_not_allowed')

    def test_a_bound_proxy_rejects_a_different_model(self):
        other = json.dumps({'model': 'glm-5.2', 'messages': [{'role': 'user', 'content': 'x'}]}).encode()
        with self.assertRaises(proxy.PolicyError) as caught:
            proxy.check_request('POST', '/inference/go/openai/v1/chat/completions', self.JSON, other, 'qwen3.8-max')
        self.assertEqual(caught.exception.code, 'model_mismatch')


if __name__ == '__main__':
    unittest.main()
