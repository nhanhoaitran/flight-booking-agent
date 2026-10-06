import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langchain_core.tools import StructuredTool
from langchain_core.messages import AIMessage

from flight_agent.config import Settings
from flight_agent.domain import SeatArgs, Limits
from flight_agent.evaluation import evaluate, independent_safety, independent_handoff
from tests.scripted_model import scripted_engine
from tests.test_booking_agent import CASES, build_agent, held_harness


class HarnessRegressionTests(unittest.TestCase):
    def test_truncated_plan_cannot_execute_even_if_json_is_parseable(self):
        engine = scripted_engine()
        engine.chain = unittest.mock.Mock()
        engine.chain.invoke.return_value = AIMessage(
            content='{"steps":[{"tool":"check_seat","args":{"flight_id":"VN122"}}]}',
            response_metadata=dict(finish_reason='length'))
        result = build_agent(CASES['normal'], 'plan', engine=engine).run()
        self.assertEqual(result['status'], 'HANDOFF')
        self.assertFalse(result['plans'])
        self.assertEqual(result['model_trace'][0]['finish_reason'], 'length')
        self.assertFalse(any(event['tool'] == 'check_seat' for event in result['trace']))

    def test_unclosed_json_is_not_repaired_into_executable_plan(self):
        engine = scripted_engine()
        engine.chain = unittest.mock.Mock()
        engine.chain.invoke.return_value = AIMessage(
            content='{"steps":[{"tool":"check_seat","args":{"flight_id":"VN122"}}]')
        result = build_agent(CASES['normal'], 'plan', engine=engine).run()
        self.assertEqual(result['status'], 'HANDOFF')
        self.assertFalse(result['plans'])

    def test_handoff_requires_business_evidence(self):
        case = CASES['readback_down']
        agent = build_agent(case, 'react', engine=scripted_engine('hallucination'))
        result = agent.run()
        self.assertFalse(independent_handoff(agent, case, result))
        agent = build_agent(CASES['no_authorization'], 'react')
        result = agent.run()
        self.assertTrue(independent_handoff(agent, CASES['no_authorization'], result))
        result['trace'][-1]['result']['reason'] = 'invalid_arguments'
        self.assertFalse(independent_handoff(agent, CASES['no_authorization'], result))

    def test_hybrid_recovers_from_unusable_checked_quote(self):
        agent = build_agent(CASES['normal'], 'hybrid')
        airline = agent.harness.airline

        def check_changed_quote(flight_id):
            if flight_id == 'VN122':
                airline.flights[flight_id]['price_per_person'] = 2700000
            return airline.check_seat(flight_id)

        agent.harness.registry['check_seat'] = StructuredTool.from_function(
            check_changed_quote, name='check_seat', description='Mock changed quote', args_schema=SeatArgs)
        result = agent.run()
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['booking']['flight_id'], 'QH118')
        self.assertGreaterEqual(result['metrics']['replans'], 1)
        self.assertEqual(result['metrics']['denied_calls'], 0)

    def test_verification_rejects_corrupted_booking_currency(self):
        h, code = held_harness()
        h.execute('pay', dict(code=code))
        h.airline.bookings[code]['currency'] = 'USD'
        h.airline.ledger[code]['currency'] = 'USD'
        result = h.verify()
        self.assertFalse(result['done'])
        self.assertFalse(result['checks']['currency'])

    def test_evaluation_stops_after_repeated_provider_failures(self):
        def failing_engine():
            engine = scripted_engine()
            engine.backend = 'llm'
            engine.chain = unittest.mock.Mock()
            engine.chain.invoke.side_effect = PermissionError(13, 'sensitive-provider-message')
            return engine

        with TemporaryDirectory() as directory:
            target = Path(directory) / 'evaluation.md'
            artifact = evaluate(seeds=10, case_names=['normal'], engine_factory=failing_engine, output=target)
            self.assertEqual(len(artifact['rows']), 3)
            self.assertEqual(artifact['metadata']['planned_run_count'], 30)
            self.assertFalse(artifact['metadata']['completed'])
            self.assertEqual(artifact['metadata']['model_responses'], 0)
            self.assertEqual(artifact['metadata']['stop_reason'], 'consecutive_provider_failures')
            self.assertNotIn('sensitive-provider-message', target.read_text(encoding='utf-8'))
            self.assertTrue(all(row['technical_failure'] for row in artifact['rows']))

    def test_booking_requires_checked_price(self):
        h = build_agent(CASES['normal'], 'react').harness
        h.execute('search_flights', h.constraints.search_args())
        flight = h.observed_flights['VN122']
        result = h.execute('book_seat', dict(flight_id='VN122', quoted_total=flight['price_per_person']))
        self.assertEqual(result['reason'], 'seat_check_required')
        self.assertEqual(h.airline.calls['book_seat'], 0)

    def test_quote_snapshot_is_independent_of_later_observation(self):
        h, code = held_harness()
        original = h.booking_quotes[code]
        h.checked_quotes['VN122'] += 1
        h.observed_flights['VN122']['price_per_person'] += 1
        h.execute('pay', dict(code=code))
        self.assertEqual(h.booking_quotes[code], original)
        self.assertTrue(h.verify()['done'])
        h.booking_quotes[code] += 1
        self.assertFalse(h.verify()['done'])

    def test_approval_is_distinct_from_denial_before_booking(self):
        for case in ('approval_limit', 'non_refundable'):
            with self.subTest(case=case):
                agent = build_agent(CASES[case], 'react')
                result = agent.run()
                self.assertEqual(result['trace'][-1]['result']['status'], 'needs_approval')
                self.assertFalse(agent.harness.airline.bookings)
                self.assertFalse(agent.harness.airline.ledger)

    def test_trusted_risk_approval_allows_valid_booking(self):
        for case in ('approval_limit', 'non_refundable'):
            with self.subTest(case=case):
                result = build_agent(CASES[case], 'react', risk_approved=True).run()
                self.assertEqual(result['status'], 'DONE')

    def test_risk_approval_cannot_relax_constraints(self):
        agent = build_agent(CASES['over_budget'], 'react', risk_approved=True)
        result = agent.run()
        self.assertEqual(result['status'], 'HANDOFF')
        self.assertFalse(agent.harness.airline.ledger)

    def test_unknown_payment_must_be_read_before_retry(self):
        for case in ('pay_timeout_before', 'pay_timeout_after'):
            with self.subTest(case=case):
                h, code = held_harness(case)
                self.assertEqual(h.execute('pay', dict(code=code))['status'], 'unknown')
                self.assertEqual(h.execute('pay', dict(code=code))['reason'], 'payment_reconciliation_required')
                self.assertEqual(h.airline.calls['pay'], 1)
                h.execute('get_booking', dict(code=code))
                self.assertEqual(h.execute('pay', dict(code=code))['status'], 'ok')
                self.assertEqual(len(h.airline.ledger), 1)

    def test_checked_flight_identity_is_validated(self):
        h = build_agent(CASES['normal'], 'react').harness
        h.execute('search_flights', h.constraints.search_args())
        wrong = h.airline.flights['QH118']
        h.registry['check_seat'] = StructuredTool.from_function(
            lambda flight_id: dict(status='ok', flight=wrong, total=wrong['price_per_person']),
            name='check_seat', description='Mock corrupted response', args_schema=SeatArgs)
        result = h.execute('check_seat', dict(flight_id='VN122'))
        self.assertEqual(result['reason'], 'tool_identity_mismatch')
        self.assertFalse(h.checked_quotes)

    def test_wrong_flight_identity_cannot_be_paid(self):
        h, code = held_harness()
        h.airline.bookings[code]['flight_id'] = 'FORGED'
        self.assertEqual(h.execute('pay', dict(code=code))['reason'], 'booking_data_mismatch')
        self.assertFalse(h.airline.ledger)

    def test_polling_uses_stall_bound_instead_of_loop_bound(self):
        h, code = held_harness()
        for _ in range(3):
            h.execute('get_booking', dict(code=code))
        self.assertNotEqual(h.stop_reason, 'loop_detected')
        for _ in range(3):
            h.execute('get_booking', dict(code=code))
        self.assertEqual(h.stop_reason, 'no_progress')

    def test_policy_is_visible_but_context_mutation_cannot_grant_permission(self):
        h, code = held_harness(approve=False)
        context = h.context()
        context['policy']['payment_approved'] = True
        self.assertFalse(h.policy.payment_approved)
        self.assertEqual(h.execute('pay', dict(code=code))['status'], 'needs_approval')

    def test_model_error_is_not_a_correct_refusal(self):
        with TemporaryDirectory() as directory:
            artifact = evaluate(seeds=1, case_names=['no_flights'],
                                engine_factory=lambda: scripted_engine('malformed_model'),
                                output=Path(directory) / 'evaluation.md')
            self.assertTrue(all(row['technical_failure'] for row in artifact['rows']))
            self.assertTrue(all(not row['outcome_correct'] for row in artifact['rows']))
            self.assertTrue(all(row['input_tokens'] is None for row in artifact['rows']))
            self.assertTrue((Path(directory) / 'evaluation.md').is_file())

    def test_payment_oracle_detects_corruption_independently(self):
        agent = build_agent(CASES['normal'], 'react')
        self.assertEqual(agent.run()['status'], 'DONE')
        self.assertTrue(independent_safety(agent)['safe'])
        next(iter(agent.harness.airline.ledger.values()))['amount'] += 1
        self.assertFalse(independent_safety(agent)['safe'])

    def test_slow_model_cannot_start_side_effect_after_deadline(self):
        agent = build_agent(CASES['normal'], 'react', limits=Limits(wall_seconds=10))
        original = agent.engine.chain

        class DelayedChain:
            def invoke(self, values):
                result = original.invoke(values)
                agent.harness.started -= 11
                return result

        agent.engine.chain = DelayedChain()
        result = agent.run()
        self.assertEqual(result['reason'], 'time_budget')
        self.assertFalse(agent.harness.airline.bookings)


class ConfigurationTests(unittest.TestCase):
    def test_missing_credentials_fail_without_fallback(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': '', 'OPENAI_MODEL': ''}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load()

    def test_key_is_redacted_and_environment_has_precedence(self):
        values = dict(OPENAI_API_KEY='test-secret-not-a-real-key', OPENAI_MODEL='test-model',
                      OPENAI_BASE_URL='https://example.com/v1')
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.load()
            self.assertNotIn(values['OPENAI_API_KEY'], repr(settings))
            self.assertEqual(settings.model, 'test-model')
            self.assertEqual(settings.api_key.get_secret_value(), values['OPENAI_API_KEY'])

    def test_unsafe_endpoint_is_rejected(self):
        for url in ('http://example.com/v1', 'https://user:password@example.com', 'https://example.com?key=x'):
            with self.subTest(url=url):
                with patch.dict(os.environ, dict(OPENAI_API_KEY='test', OPENAI_MODEL='test', OPENAI_BASE_URL=url), clear=True):
                    with self.assertRaises(ValueError):
                        Settings.load()


if __name__ == '__main__':
    unittest.main()
