"""Guard lifecycle validation against REAL OpenCode 1.19.3 transcripts (tests v1.0).

The fixtures in fixtures/opencode_1_19_3 were produced by the genuine 1.19.3 binary (see
capture.py there for the exact reproduction). Only the model's replies were scripted.
"""
import copy
import importlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
guard = importlib.import_module('opencode_guard')
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'opencode_1_19_3'


def load(name):
    lines = (FIXTURES / f'{name}.jsonl').read_text().splitlines()
    meta = json.loads((FIXTURES / f'{name}.meta.json').read_text())
    return lines, meta


def verdict(name):
    lines, meta = load(name)
    return guard.evaluate(lines, meta['exit_code'], meta['timed_out'])


def events(name):
    return [json.loads(line) for line in load(name)[0]]


def dump(rows):
    return [json.dumps(row) for row in rows]


class RealTranscriptTests(unittest.TestCase):
    def test_fixtures_are_from_opencode_1_19_3(self):
        for meta_file in FIXTURES.glob('*.meta.json'):
            self.assertEqual(json.loads(meta_file.read_text())['opencode_version'], '1.19.3', meta_file.name)
        # 9th fixture: success_ok, a real 1.19.3 capture of the exact-OK probe.
        self.assertEqual(len(list(FIXTURES.glob('*.jsonl'))), 9)

    def test_real_successful_transcripts_are_accepted(self):
        self.assertEqual(verdict('success_text'), ('success', ''))
        self.assertEqual(verdict('success_edit'), ('success', ''))

    def test_real_multi_step_success_has_a_valid_intermediate_tool_calls_step(self):
        rows = events('success_edit')
        reasons = [r['part']['reason'] for r in rows if r['type'] == 'step_finish']
        self.assertEqual(reasons, ['tool-calls', 'stop'])
        self.assertEqual(len({r['sessionID'] for r in rows}), 1)

    def test_real_failures_are_rejected_with_the_expected_reason(self):
        expected = {
            'fail_provider_500': 'nonzero_exit:1',      # exit 1 + error event after retries
            'fail_unauthorized_401': 'nonzero_exit:1',
            'fail_ends_on_tool_calls': 'nonzero_exit:1',  # tool-calls step, then provider error
            'fail_truncated_length': 'incomplete_generation:length',  # exit 0, no error event
            'fail_stream_cut': 'timeout',               # endless retry loop killed by timeout
            'fail_timeout_hang': 'timeout',
        }
        for name, reason in expected.items():
            status, why = verdict(name)
            self.assertEqual((status, why), ('failed', reason), name)

    def test_real_failures_would_be_rejected_even_if_exit_status_were_ignored(self):
        # exit 0 is what a wrapper that only checks "did it print a terminal event" would see
        for name in ('fail_provider_500', 'fail_unauthorized_401', 'fail_ends_on_tool_calls',
                     'fail_truncated_length', 'fail_stream_cut'):
            lines, _meta = load(name)
            status, why = guard.evaluate(lines, 0, False)
            self.assertEqual(status, 'failed', name)
            self.assertNotEqual(why, '', name)
        # and the error event itself is what condemns the provider failures
        for name in ('fail_provider_500', 'fail_unauthorized_401', 'fail_ends_on_tool_calls'):
            self.assertEqual(guard.evaluate(load(name)[0], 0, False), ('failed', 'agent_reported_error'), name)

    def test_real_truncation_exits_zero_so_the_exit_code_alone_is_not_enough(self):
        _lines, meta = load('fail_truncated_length')
        self.assertEqual(meta['exit_code'], 0)
        self.assertEqual(verdict('fail_truncated_length')[0], 'failed')

    def test_real_event_shape_matches_the_validator_assumptions(self):
        for row in events('success_edit'):
            self.assertEqual(set(row) - {'part'}, {'type', 'timestamp', 'sessionID'})
            self.assertTrue(row['sessionID'].startswith('ses_'))
            self.assertTrue(row['part']['messageID'].startswith('msg_'))
            self.assertEqual(row['part']['sessionID'], row['sessionID'])


class ContradictionAndOrderingTests(unittest.TestCase):
    """Mutations of a real successful transcript. A valid terminal event must not rescue them."""

    def mutate(self, fn, name='success_edit'):
        rows = copy.deepcopy(events(name))
        fn(rows)
        return guard.evaluate(dump(rows), 0, False)

    def test_baseline_is_accepted(self):
        self.assertEqual(self.mutate(lambda rows: None), ('success', ''))

    def test_error_event_anywhere_contradicts_success(self):
        error = {'type': 'error', 'timestamp': 1792515018000, 'sessionID': events('success_edit')[0]['sessionID'],
                 'error': {'name': 'APIError', 'data': {'message': 'x'}}}
        for position in (0, 2, -1):
            status, why = self.mutate(lambda rows, p=position: rows.insert(p if p >= 0 else len(rows), error))
            self.assertEqual((status, why), ('failed', 'agent_reported_error'), position)

    def test_session_identity_must_be_consistent(self):
        def other_session(rows):
            rows[3]['sessionID'] = 'ses_zzzzzzzzzzzzzzzzzzzz'
            rows[3]['part']['sessionID'] = 'ses_zzzzzzzzzzzzzzzzzzzz'
        self.assertEqual(self.mutate(other_session), ('failed', 'session_mismatch'))

        def part_mismatch(rows):
            rows[1]['part']['sessionID'] = 'ses_zzzzzzzzzzzzzzzzzzzz'
        self.assertEqual(self.mutate(part_mismatch), ('failed', 'malformed_part'))

        def bad_first(rows):
            for row in rows:
                row['sessionID'] = 'not-a-session'
                row['part']['sessionID'] = 'not-a-session'
        self.assertEqual(self.mutate(bad_first), ('failed', 'session_identity_missing'))

    def test_unmatched_and_unfinished_steps_rejected(self):
        self.assertEqual(self.mutate(lambda rows: rows.pop()), ('failed', 'unfinished_step'))   # last step_finish missing
        self.assertEqual(self.mutate(lambda rows: rows.pop(2)), ('failed', 'event_ordering'))   # first finish missing
        def step_after_stop(rows):
            extra = copy.deepcopy(rows[3])
            extra['timestamp'] += 1
            extra['part']['messageID'] = 'msg_ffffffffffffffffffffff'
            rows.append(extra)
        self.assertEqual(self.mutate(step_after_stop), ('failed', 'event_ordering'))            # step after stop
        self.assertEqual(self.mutate(lambda rows: rows.insert(1, copy.deepcopy(rows[0]))),
                         ('failed', 'event_ordering'))                                          # nested step_start

    def test_trailing_unfinished_events_rejected(self):
        def trailing_text(rows):
            extra = copy.deepcopy(rows[4])           # a text event after the final step_finish
            extra['timestamp'] += 1
            rows.append(extra)
        self.assertEqual(self.mutate(trailing_text), ('failed', 'event_ordering'))

        def trailing_start(rows):
            extra = copy.deepcopy(rows[3])
            extra['timestamp'] += 1
            extra['part']['messageID'] = 'msg_ffffffffffffffffffffff'
            rows.append(extra)
        self.assertEqual(self.mutate(trailing_start), ('failed', 'event_ordering'))

    def test_message_and_step_relationships_enforced(self):
        def foreign_message(rows):
            rows[1]['part']['messageID'] = 'msg_ffffffffffffffffffffff'
        self.assertEqual(self.mutate(foreign_message), ('failed', 'event_ordering'))

        def finish_other_message(rows):
            rows[2]['part']['messageID'] = 'msg_ffffffffffffffffffffff'
        self.assertEqual(self.mutate(finish_other_message), ('failed', 'unmatched_step_finish'))

        def missing_message(rows):
            del rows[1]['part']['messageID']
        self.assertEqual(self.mutate(missing_message), ('failed', 'malformed_part'))

    def test_impossible_ordering_rejected(self):
        self.assertEqual(self.mutate(lambda rows: rows.reverse())[0], 'failed')
        self.assertEqual(self.mutate(lambda rows: rows.__setitem__(slice(0, 3), [rows[1], rows[0], rows[2]])),
                         ('failed', 'event_ordering'))

        def time_travel(rows):
            rows[4]['timestamp'] = 5
        self.assertEqual(self.mutate(time_travel), ('failed', 'timestamp_order'))

    def test_finish_reason_must_match_the_step_content(self):
        def stop_with_tools(rows):
            rows[2]['part']['reason'] = 'stop'      # first step ran a tool but claims a clean stop
        self.assertEqual(self.mutate(stop_with_tools), ('failed', 'stop_with_pending_tools'))

        def tool_calls_without_tools(rows):
            rows[-1]['part']['reason'] = 'tool-calls'
        self.assertEqual(self.mutate(tool_calls_without_tools, 'success_text'),
                         ('failed', 'tool_calls_without_tools'))
        for reason in ('length', 'unknown', 'error', 'content-filter', 'other', '', None):
            def set_reason(rows, r=reason):
                rows[-1]['part']['reason'] = r
            status, why = self.mutate(set_reason, 'success_text')
            self.assertEqual(status, 'failed', str(reason))
            self.assertTrue(why.startswith('incomplete_generation'), why)

    def test_unsupported_termination_and_unknown_types_rejected(self):
        def only_tool_calls(rows):
            del rows[3:]
        self.assertEqual(self.mutate(only_tool_calls), ('failed', 'no_supported_termination'))

        def strange(rows):
            rows[1]['type'] = 'subagent_spawn'
        self.assertEqual(self.mutate(strange), ('failed', 'unknown_event_type'))

        def pending_tool(rows):
            rows[1]['part']['state']['status'] = 'running'
        self.assertEqual(self.mutate(pending_tool), ('failed', 'tool_state_unfinished'))

    def test_a_valid_terminal_event_alone_is_not_enough(self):
        rows = events('success_text')
        terminal_only = [dump(rows)[-1]]
        self.assertEqual(guard.evaluate(terminal_only, 0, False)[0], 'failed')
        self.assertEqual(guard.evaluate(terminal_only + dump(rows), 0, False)[0], 'failed')

    def test_nonzero_exit_or_timeout_overrides_a_complete_transcript(self):
        lines = load('success_edit')[0]
        self.assertEqual(guard.evaluate(lines, 1, False), ('failed', 'nonzero_exit:1'))
        self.assertEqual(guard.evaluate(lines, 0, True), ('failed', 'timeout'))
        self.assertEqual(guard.evaluate(lines, None, False)[0], 'failed')

    def test_garbage_and_non_objects_rejected(self):
        lines = load('success_text')[0]
        for junk in ('{"type": ', '[1,2]', '"x"', 'null', 'Traceback (most recent call last):', '{"type": 5}'):
            self.assertEqual(guard.evaluate(lines + [junk], 0, False)[0], 'failed', junk)
        self.assertEqual(guard.evaluate([], 0, False), ('failed', 'no_events'))

    def test_session_extraction_and_final_text_use_the_real_shape(self):
        lines = load('success_edit')[0]
        self.assertTrue(guard.extract_session(lines).startswith('ses_'))
        self.assertEqual(guard.extract_final_text(lines), 'Created hello.txt.')


if __name__ == '__main__':
    unittest.main()
