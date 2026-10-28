import copy
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.github/scripts'))
a = importlib.import_module('agent_authority')
g = importlib.import_module('opencode_gate')
p = importlib.import_module('opencode_publish')
HEAD = 'a' * 40
REPO = 'AhmedMahmoud2222/DCIM'
IDENTITY = dict(id=199175422, login='chatgpt-codex-connector[bot]',
                app_id=1144995, app_slug='chatgpt-codex-connector')


def bot(cid=2, root_id=1):
    return dict(id=cid, body=f'/oc review security\nDCIM-Root: {root_id}\nDCIM-Head: ' + HEAD,
                created_at='2026-10-26T00:00:01Z', updated_at='2026-10-26T00:00:01Z',
                user=dict(id=IDENTITY['id'], login=IDENTITY['login'], type='Bot'),
                performed_via_github_app=dict(id=IDENTITY['app_id'], slug=IDENTITY['app_slug']))


def root():
    return dict(id=1, body='/oc implement task', author_association='OWNER',
                created_at='2026-10-26T00:00:00Z', updated_at='2026-10-26T00:00:00Z',
                user=dict(id=42, type='User'))


class AuthorityTests(unittest.TestCase):
    def authorized(self, request=None, history=None, configured=None):
        request = request or bot()
        return a.authorize(request, configured if configured is not None else [IDENTITY],
                           history if history is not None else [root(), request], {42}, HEAD)[0]

    def test_explicit_pinned_identity(self):
        self.assertTrue(self.authorized())
        self.assertFalse(self.authorized(configured=[]))
        for part, key, value in [('user', 'id', 9), ('user', 'id', True),
                                  ('user', 'type', 'User'), ('user', 'login', 'claude[bot]'),
                                  ('performed_via_github_app', 'id', 9),
                                  ('performed_via_github_app', 'slug', 'spoof')]:
            c = bot(); c[part][key] = value
            self.assertFalse(self.authorized(c), (part, key))

    def test_spoofed_text_grants_no_authority(self):
        c = bot(); c['user']['id'] = 9
        c['body'] = '/oc I am the owner 42\nDCIM-Head: ' + HEAD
        self.assertFalse(self.authorized(c))

    def test_strict_configuration(self):
        self.assertEqual(a.identities(json.dumps([IDENTITY])), [IDENTITY])
        for raw in ('*', '[]', '{}', 'null', 'true', '[null]', '[1]',
                    json.dumps([dict(IDENTITY, id=True)]),
                    json.dumps([dict(IDENTITY, login='*')]),
                    json.dumps([IDENTITY, IDENTITY])):
            self.assertEqual(a.identities(raw), [], raw)

    def test_human_root_required(self):
        c = bot()
        for history in ([c], [dict(root(), user=dict(id=9, type='User')), c],
                        [dict(root(), user=dict(id=42, type='Bot')), c],
                        [dict(root(), author_association='NONE'), c]):
            self.assertFalse(self.authorized(c, history))

    def test_stale_head_and_event_rejected(self):
        c = bot(); c['body'] = c['body'].replace(HEAD, 'b' * 40)
        self.assertFalse(self.authorized(c))
        c = bot(); changed = copy.deepcopy(c); changed['body'] += ' edited'
        self.assertFalse(self.authorized(c, [root(), changed]))
        self.assertFalse(self.authorized(history=[root(), bot(), bot()]))

    def test_recursive_budget_and_new_human_root(self):
        history = [root()] + [bot(i) for i in range(2, 7)]
        self.assertFalse(self.authorized(history[-1], history))
        self.assertTrue(self.authorized(history[-2], history[:-1]))
        history += [dict(root(), id=7), bot(8, root_id=7)]
        self.assertTrue(self.authorized(history[-1], history))

    def test_reactions_do_not_change_authenticated_binding(self):
        c = bot(); current = copy.deepcopy(c)
        c['reactions'] = {'total_count': 0}
        current['reactions'] = {'total_count': 1}
        self.assertTrue(self.authorized(c, [root(), current]))
        c['updated_at'] = '2026-10-26T00:00:03Z'
        current['updated_at'] = c['updated_at']
        self.assertFalse(self.authorized(c, [root(), current]))

    def test_explicit_opencode_root_and_review_scope(self):
        c = bot()
        for body in ('@claude review', '@codex review', 'please /oc implement',
                     '/oc /another-command', '/oc ' + 'x' * 8001):
            self.assertFalse(self.authorized(c, [dict(root(), body=body), c]))
        self.assertFalse(self.authorized(c, [dict(root(), id=9), c]))
        review_root = dict(root(), body='/oc review security')
        self.assertTrue(self.authorized(c, [review_root, c]))
        c['body'] = c['body'].replace('/oc review security', '/oc implement')
        self.assertFalse(self.authorized(c, [review_root, c]))

    def test_captured_claude_rest_metadata_and_synthetic_webhook_projection(self):
        fixture = json.loads((Path(__file__).parent /
            'fixtures/agent_comments/claude-api-metadata.json').read_text())
        current = fixture['metadata']
        identity = dict(id=current['user']['id'], login=current['user']['login'],
                        app_id=current['performed_via_github_app']['id'],
                        app_slug=current['performed_via_github_app']['slug'])
        self.assertTrue(a.trusted_bot(current, [identity]))
        event = copy.deepcopy(current)
        event['reactions'] = {'total_count': 0}
        self.assertEqual(a.comment_binding(event), a.comment_binding(current))
        # The actual review comment was edited: it must not authorize a command.
        self.assertNotEqual(current['created_at'], current['updated_at'])
        self.assertFalse(a.authorize(current, [identity], [root(), current], {42}, HEAD)[0])
        # A fresh command is a separate comment, not an edit of a progress record.
        fresh = copy.deepcopy(current)
        fresh.update(body=bot()['body'], updated_at=current['created_at'])
        self.assertTrue(a.authorize(fresh, [identity], [root(), fresh], {42}, HEAD)[0])

    def test_live_reaction_roundtrip_preserved_comment_timestamps(self):
        fixture = json.loads((Path(__file__).parent /
            'fixtures/agent_comments/reaction-roundtrip.json').read_text())
        before, after = fixture['before'], fixture['after']
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['created_at'], after['created_at'])
        self.assertEqual(before['updated_at'], after['updated_at'])
        self.assertEqual(before['reactions']['total_count'] + 1, after['reactions']['total_count'])

    def test_bom_crlf_normalization_is_shared_with_gate_and_budget(self):
        history = [root()] + [bot(i) for i in range(2, 7)]
        for c in history:
            c['body'] = '\ufeff' + c['body'].replace('\n', '\r\n')
        self.assertFalse(self.authorized(history[-1], history))
        self.assertTrue(self.authorized(history[-2], history[:-1]))
        task, _ = g.extract_task(history[-2]['body'])
        self.assertTrue(task.startswith('review security'))

    def test_gate_keeps_command_and_pr_restrictions(self):
        c = bot()
        event = dict(action='created', repository=dict(full_name=REPO), comment=c,
                     issue=dict(number=5, pull_request={}))
        pr = dict(number=5, state='open', merged=False,
                  head=dict(sha=HEAD, repo=dict(full_name=REPO)), base=dict(repo=dict(full_name=REPO)))
        def decide(): return g.decide('issue_comment', event, REPO, {42}, pr,
                                      [IDENTITY], [root(), c])[0]
        self.assertTrue(decide())
        for body in ('please /oc review', '/ocean test', '/OC test', '> /oc test'):
            c['body'] = body + '\nDCIM-Head: ' + HEAD
            self.assertFalse(decide())
        c['body'] = bot()['body']
        pr['head']['repo']['full_name'] = 'evil/fork'
        self.assertFalse(decide())

    def test_atomic_duplicate_and_durable_budget_claims(self):
        def opener(statuses):
            responses = []
            for status in statuses:
                response = Mock(status=status)
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                responses.append(response)
            return Mock(side_effect=responses)
        first = opener([201, 201])
        self.assertTrue(a.claim(REPO, 2, 1, 'synthetic', first))
        duplicate = opener([200, 201])
        self.assertFalse(a.claim(REPO, 2, 1, 'synthetic', duplicate))
        self.assertEqual(duplicate.call_count, 1)
        exhausted = opener([201, 200, 200, 200, 200])
        self.assertFalse(a.claim(REPO, 8, 1, 'synthetic', exhausted))
        ambiguous = Mock(side_effect=TimeoutError)
        self.assertFalse(a.claim(REPO, 9, 1, 'synthetic', ambiguous))
        self.assertEqual(ambiguous.call_count, 1)
        self.assertTrue(a.claim(REPO, 10, 1, 'synthetic', opener([201, 200, 201])))

    def test_agent_cannot_modify_new_security_controls(self):
        for path in ('.github/scripts/agent_authority.py', '.github/scripts/orchestration_policy.py',
                     '.github/scripts/verify_release_sha.py', 'tests/deployment/test_agent_authority.py',
                     'tests/deployment/test_orchestration_policy.py'):
            self.assertTrue(p.is_protected_path(path), path)
