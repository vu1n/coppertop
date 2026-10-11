import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from coppertop.chords import LocalStore, canonical, frame_id
from coppertop.decision_gate import GatePolicy, validate_answers
from coppertop.decision_provider import DecisionProvider, ProviderError, BoundScorer, OwnedPost, post_json
from coppertop.decision_recording import prepare_case, provider_body, save_judgment, png_rgb, array
from coppertop.decision_runner import gated_rollout
from coppertop.official_recording import ACTION_LAYOUT, STATE_LAYOUT


def answers(replan=0.9):
    return {'timing': {'type': 'choice', 'choice': 'replan' if replan > 0.5 else 'continue',
                       'confidence': max(replan, 1-replan),
                       'probabilities': {'replan': replan, 'continue': 1-replan, 'uncertain': 0}},
            'data_usefulness': {'type': 'choice', 'choice': 'uncertain', 'confidence': 1,
                               'probabilities': {'useful': 0, 'not_useful': 0, 'uncertain': 1}}}


class GateTests(unittest.TestCase):
    def decision(self, value=None, **kwargs):
        return GatePolicy().decide(value or answers(), observed_frame=8,
            **dict({'current_frame': 8, 'queued_until': 16, 'remaining_queries': 1}, **kwargs))

    def test_admission_does_not_consume_or_invent_controls(self):
        self.assertTrue(self.decision()['request_early_replan'])
        for kw, reason in [({'current_frame': 11}, 'stale_decision'),
                           ({'current_frame': 16}, 'normal_queue_boundary'),
                           ({'remaining_queries': 0}, 'query_budget_exhausted'),
                           ({'stopped': True}, 'episode_stopped')]:
            result = self.decision(**kw)
            self.assertFalse(result['request_early_replan'])
            self.assertEqual(result['reason'], reason)
        self.assertFalse(self.decision(answers(0.7))['request_early_replan'])

    def test_malformed_provider_answers_rejected(self):
        mutations = [lambda x: x['timing']['probabilities'].update(replan=float('nan')),
                     lambda x: x['timing']['probabilities'].update(replan=True),
                     lambda x: x['timing']['probabilities'].update(replan=0.8),
                     lambda x: x['timing'].update(choice='invented_skill'),
                     lambda x: x['timing'].update(choice='continue'),
                     lambda x: x.pop('data_usefulness')]
        for mutate in mutations:
            value = answers()
            mutate(value)
            with self.assertRaises((ValueError, KeyError)):
                validate_answers(value)

    def test_usefulness_does_not_trigger_intervention(self):
        value = answers(0.1)
        value['data_usefulness'] = {'type': 'choice', 'choice': 'useful', 'confidence': 1,
                                   'probabilities': {'useful': 1, 'not_useful': 0, 'uncertain': 0}}
        self.assertFalse(self.decision(value)['request_early_replan'])

    def test_png_preserves_exact_rgb(self):
        image = np.arange(256*256*3, dtype=np.uint8).reshape(256, 256, 3)
        with av.open(io.BytesIO(png_rgb(image)), format='png_pipe') as container:
            decoded = next(container.decode(video=0)).to_ndarray(format='rgb24')
        self.assertTrue(np.array_equal(decoded, image))


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = LocalStore(Path(self.temp.name)/'source')
        self.output = LocalStore(Path(self.temp.name)/'decisions')
        self.chunk = np.arange(16*12, dtype=np.float32).reshape(16, 12)/100
        out = io.BytesIO()
        np.save(out, self.chunk, allow_pickle=False)
        chunk = {'object': self.source.put(out.getvalue()), 'shape': [16,12], 'dtype': 'float32'}
        image = np.zeros((256,256,3), dtype=np.uint8)
        out = io.BytesIO()
        np.save(out, image, allow_pickle=False)
        ref = {'object': self.source.put(out.getvalue()), 'shape': [256,256,3], 'dtype': 'uint8'}
        observation = self.source.put(canonical({'native': {name: ref for name in
            ('video.robot0_agentview_left','video.robot0_agentview_right','video.robot0_eye_in_hand')}}))
        self.rows = [{'step': i, 'time_ns': i*50_000_000, 'frame_id': frame_id('test', i),
                     'state': [float(i)]*16, 'observation': observation,
                     'action': self.chunk[i].tolist(), 'success': i==9,
                     'query_id': 0, 'chunk_offset': i,
                     'terminated': False, 'truncated': False, 'reward': 10000 if i==9 else 0}
                    for i in range(10)]
        self.record = {'kind': 'generated_official_rollout', 'episode_id': 'test',
            'initialization_group': 'group', 'instruction': 'Place the mug',
            'state_layout': STATE_LAYOUT, 'action_layout': ACTION_LAYOUT,
            'coordinate_conventions': {'state': 'native', 'action': 'native commands'},
            'provenance': {'split': 'development'},
            'queries': self.source.put(canonical({'queries': [{'at_frame': 0, 'chunk': chunk},
                {'at_frame': 8, 'chunk': {**chunk, 'object': 'f'*64}}]}))}
        self.release, self.record_id = self.publish()

    def publish(self):
        out = io.BytesIO()
        schema = pa.schema([('step',pa.int64()),('time_ns',pa.int64()),('frame_id',pa.string()),
            ('state',pa.list_(pa.float64(),16)),('observation',pa.string()),
            ('action',pa.list_(pa.float32())),('query_id',pa.int64()),('chunk_offset',pa.int64()),
            ('reward',pa.float64()),('success',pa.bool_()),('terminated',pa.bool_()),('truncated',pa.bool_())])
        pq.write_table(pa.Table.from_pylist(self.rows, schema=schema), out)
        record = dict(self.record, signals=self.source.put(out.getvalue()))
        key = self.source.put(canonical(record))
        return self.source.put(canonical({'episodes': [key]})), key

    def case(self):
        return prepare_case(self.source, self.release, self.record_id, 8, self.output)

    def test_case_is_causal_and_reproducible(self):
        key = self.case()
        self.assertEqual(self.case(), key)
        first = json.loads(self.output.get(key))
        self.rows[9]['state'] = [999]*16
        self.rows[9]['success'] = False
        self.rows[9]['reward'] = -10000
        self.release, self.record_id = self.publish()
        second = json.loads(self.output.get(self.case()))
        self.assertEqual(first['state'], second['state'])
        self.assertEqual(first['images'], second['images'])
        self.assertEqual(first['state']['queued_predictions'], self.chunk[8:].tolist())
        self.assertEqual(first['state']['past_executed_actions'][-1]['action'], self.chunk[7].tolist())
        for forbidden in ('reward', 'success', 'official_result', 'terminated', 'truncated'):
            self.assertNotIn(forbidden, first['state'])

    def test_release_frame_and_split_guards(self):
        with self.assertRaises(ValueError):
            prepare_case(self.source, self.release, 'e'*64, 8, self.output)
        for frame in (0,9,-1,True):
            with self.assertRaises(ValueError):
                prepare_case(self.source, self.release, self.record_id, frame, self.output)
        self.record['provenance']['split'] = 'sealed_test'
        self.release, self.record_id = self.publish()
        with self.assertRaises(ValueError):
            self.case()

    def test_text_and_vision_are_distinct_arms(self):
        key = self.case()
        text = provider_body(self.output, key, 'typesafe/jev-1.13', vision=False)
        vision = provider_body(self.output, key, 'clef-flash', vision=True)
        self.assertNotIn('images', text)
        self.assertEqual(len(vision['images']), 3)
        self.assertNotEqual(text['state']['input_modality'], vision['state']['input_modality'])

    def provider(self, **kwargs):
        return DecisionProvider('cloudflare', 'test-not-a-credential', 'a'*32, **kwargs)

    def response(self):
        return {'success': True, 'result': {'model': 'clef-flash', 'answers': answers(),
                                          'usage': {'input_tokens': 200, 'output_tokens': 0}}}

    def test_offline_preview_no_transport(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('Network called')
        result = self.provider().score(self.output, self.case(), transport=forbidden)
        self.assertEqual(result['provider_calls'], 0)
        self.assertNotIn('test-not-a-credential', repr(self.provider()))

    def test_single_call_receipt_and_overlay_binding(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(args[0])
            return self.response()
        key = self.case()
        receipt = self.provider().score(self.output, key, execute=True, transport=transport)
        overlay = json.loads(self.output.get(save_judgment(self.output, key, receipt)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(overlay['label_status'], 'model_prediction_not_ground_truth')
        self.assertFalse(receipt['provider_revision_pinned'])
        with self.assertRaises(ValueError):
            save_judgment(self.output, key, dict(receipt, case_id='b'*64))

    def test_provider_failure_no_retry(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(1)
            return {'success': False, 'errors': ['SECRET should never be logged']}
        with self.assertRaises(ProviderError) as error:
            self.provider().score(self.output, self.case(), execute=True, transport=transport)
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(calls, [1])

    def test_served_identity_and_latest_guards(self):
        with self.assertRaises(ValueError):
            self.provider(model='latest')
        response = self.response()
        response['result']['model'] = 'unrelated'
        with self.assertRaises(ProviderError):
            self.provider().score(self.output, self.case(), execute=True, transport=lambda *a, **k: response)

    def test_jev_response_preserves_revision_and_cost(self):
        response = {'model':'typesafe/jev-1.13-20260917','answers':answers(),
                    'usage':{'input_tokens':200,'cost':0.00003}}
        result = DecisionProvider('openrouter', 'test-not-a-credential').score(self.output,
            self.case(), execute=True, transport=lambda *a, **k: response)
        self.assertEqual(result['served_model'], response['model'])
        self.assertEqual(result['usage']['cost_usd'], 0.00003)

    def test_bound_scorer_cannot_repeat(self):
        scorer = BoundScorer(self.provider(), self.output, self.case())
        scorer.transport = lambda *a, **k: self.response()
        self.assertEqual(scorer(), answers())
        with self.assertRaises(ValueError):
            scorer()

    def test_npy_header_checked_before_allocation(self):
        out = io.BytesIO()
        np.lib.format.write_array_header_1_0(out, {'descr': '<f4', 'fortran_order': False, 'shape': (10**10,)})
        ref = {'object': self.source.put(out.getvalue()), 'shape': [10**10], 'dtype': 'float32'}
        with patch('coppertop.decision_recording.np.load') as load:
            with self.assertRaises(ValueError):
                array(self.source, ref)
            load.assert_not_called()

    def test_image_count_checked_before_read(self):
        case = json.loads(self.output.get(self.case()))
        case['images'] *= 100
        key = self.output.put(canonical(case))
        with self.assertRaises(ValueError):
            provider_body(self.output, key, 'clef-flash', vision=True)

    def test_parquet_dictionary_expansion_rejected_before_decoding(self):
        rows = [dict(item, frame_id='x'*100000) for item in self.rows]
        schema = pq.ParquetFile(io.BytesIO(self.source.get(json.loads(self.source.get(self.record_id))['signals']))).schema_arrow
        out = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), out, use_dictionary=True)
        record = dict(json.loads(self.source.get(self.record_id)), signals=self.source.put(out.getvalue()))
        episode = self.source.put(canonical(record))
        release = self.source.put(canonical({'episodes': [episode]}))
        with self.assertRaises(ValueError):
            prepare_case(self.source, release, episode, 8, self.output)

    def test_failed_calls_have_distinct_attempt_receipts(self):
        case = self.case()
        failures = []
        def fail(*args, **kwargs):
            raise ProviderError('failure')
        for _ in range(2):
            scorer = BoundScorer(self.provider(), self.output, case)
            scorer.transport = fail
            with self.assertRaises(ProviderError):
                scorer()
            failures.append(scorer.failure_id)
        self.assertNotEqual(*failures)


class TransportTests(unittest.TestCase):
    def test_cancel_during_connect_cannot_dispatch(self):
        owner = OwnedPost()
        def cancel_during_connect(connection):
            owner.cancel()
        with patch('coppertop.decision_provider.http.client.HTTPSConnection.connect', cancel_during_connect), \
                patch('coppertop.decision_provider.http.client.HTTPSConnection.request') as request:
            with self.assertRaises(ProviderError):
                owner('https://openrouter.ai/api/v1/systemone', 'test', {}, timeout_seconds=1)
            request.assert_not_called()

    def test_redirect_refused_no_second_send(self):
        connection = unittest.mock.Mock()
        connection.getresponse.return_value.status = 302
        with patch('coppertop.decision_provider.http.client.HTTPSConnection', return_value=connection):
            with self.assertRaises(ProviderError):
                post_json('https://openrouter.ai/api/v1/systemone', 'test', {}, timeout_seconds=1)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_credentials_never_sent_to_other_hosts(self):
        with patch('coppertop.decision_provider.http.client.HTTPSConnection') as connection:
            with self.assertRaises(ValueError):
                post_json('https://attacker.example/', 'test', {}, timeout_seconds=1)
            connection.assert_not_called()


class OwnedCall:
    def __init__(self, value, delay=0):
        self.value, self.delay, self.calls = value, delay, 0
    def __call__(self, *args):
        self.calls += 1
        time.sleep(self.delay)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value
    def cancel(self):
        pass


class RunnerTests(unittest.TestCase):
    def run_gate(self, scorer, query, **kwargs):
        env = SimpleNamespace(step=lambda action: SimpleNamespace(stopped=False))
        old = np.arange(16*12, dtype=np.float32).reshape(16,12)
        result = gated_rollout(env, scorer, query, {}, old, observed_frame=8, boundary=8,
            seed=12, steps=4, remaining_queries=1, control_ns=10_000_000, **kwargs)
        return old, result

    def test_handoff_discards_elapsed_candidate_offsets(self):
        candidate = np.full((16,12), -1, dtype=np.float32)
        scorer, query = OwnedCall(answers()), OwnedCall(candidate)
        old, result = self.run_gate(scorer, query)
        committed = result['committed_steps']
        self.assertIsNotNone(committed)
        self.assertGreaterEqual(committed, 1)
        self.assertTrue(np.array_equal(result['actions'][0], old[8]))
        self.assertEqual(result['query_calls'], 1)
        for tick in range(committed, 4):
            self.assertTrue(np.array_equal(result['actions'][tick], candidate[tick]))

    def test_uncertain_or_failed_gate_keeps_old_controls(self):
        for value in (answers(0.1), RuntimeError('failure')):
            scorer, query = OwnedCall(value), OwnedCall(np.zeros((16,12), dtype=np.float32))
            old, result = self.run_gate(scorer, query)
            self.assertEqual(query.calls, 0)
            self.assertIsNone(result['committed_steps'])
            self.assertTrue(np.array_equal(result['actions'], old[8:12]))

    def test_late_gate_does_not_spend_actor_call(self):
        scorer = OwnedCall(answers(), delay=0.031)
        query = OwnedCall(np.zeros((16,12), dtype=np.float32))
        old, result = self.run_gate(scorer, query)
        self.assertEqual(query.calls, 0)
        self.assertFalse(result['gate']['request_early_replan'])
        self.assertTrue(np.array_equal(result['actions'], old[8:12]))

    def test_scheduler_pause_invalidates_even_ready_candidate(self):
        clock = SimpleNamespace(monotonic_ns=time.monotonic_ns,
                                sleep=lambda delay: time.sleep(delay + 0.04))
        with patch('coppertop.decision_runner.time', clock):
            with self.assertRaisesRegex(RuntimeError, 'Scheduled control boundary missed'):
                self.run_gate(OwnedCall(answers()), OwnedCall(np.zeros((16,12), dtype=np.float32)))


if __name__ == '__main__':
    unittest.main()
