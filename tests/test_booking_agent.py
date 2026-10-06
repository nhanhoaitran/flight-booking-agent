from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from datetime import date

import unittest


from pydantic import ValidationError

from flight_agent.agents import build_agent as build_live_agent

from tests.scripted_model import scripted_engine

from flight_agent.domain import Constraints, Limits, PATTERNS, PermissionPolicy, Scenario, scenarios

from flight_agent.harness import BookingHarness, HarnessStop

from flight_agent.mock_airline import MockAirline

CASES = {s.name: s for s in scenarios()}

def build_agent(scenario, pattern, **kwargs):
    kwargs.setdefault('engine', scripted_engine())
    kwargs.setdefault('reference_date', scenario.reference_date)
    return build_live_agent(scenario, pattern, **kwargs)

def held_harness(case='normal', approve=True):
    agent = build_agent(CASES[case], 'react', approve_payment=approve)
    h = agent.harness
    found = h.execute('search_flights', h.constraints.search_args())
    flight = next(f for f in found['flights'] if not h.constraints.violations(f))
    h.execute('check_seat', dict(flight_id=flight['flight_id']))
    response = h.execute('book_seat', dict(flight_id=flight['flight_id'], quoted_total=flight['price_per_person'] * h.constraints.passengers))
    return h, response['booking']['code']


class BookingAgentTests(unittest.TestCase):
    def test_scenario_outcomes(self):
        for pattern in PATTERNS:
            for scenario in scenarios():
                with self.subTest(pattern=pattern, scenario=scenario):
                    agent = build_agent(scenario, pattern)
                    result = agent.run()
                    expected = scenario.expected
                    if pattern == 'plan' and scenario.recoverable and scenario.name != 'pay_timeout_after':
                        expected = 'HANDOFF'
                    assert result['status'] == expected
                    assert result['metrics']['charge_count'] <= 1
                    assert result['metrics']['charged_total'] <= scenario.constraints.max_total
                    if expected == 'DONE':
                        assert all(result['verification']['checks'].values())
                        assert result['booking']['ticket']
                    else:
                        assert result['handoff']['question']
                        assert {'done_so_far', 'tried', 'question', 'stop_reason', 'constraints', 'charges'} <= result['handoff'].keys()
                    if not scenario.approve_payment:
                        assert not agent.harness.airline.ledger
                    for charge in agent.harness.airline.ledger.values():
                        assert charge['passenger_id'] == scenario.constraints.passenger_id

    def test_adversarial_model_never_bypasses_harness(self):
        for behavior in ['hallucination', 'drift', 'forged_permission', 'loop', 'malformed_model']:
            for pattern in PATTERNS:
                with self.subTest(behavior=behavior, pattern=pattern):
                    agent = build_agent(CASES['normal'], pattern, engine=scripted_engine(behavior=behavior))
                    result = agent.run()
                    assert result['status'] == 'HANDOFF'
                    assert not agent.harness.airline.ledger
                    assert result['metrics']['agent_tool_calls'] <= 30

    def test_no_payment_without_explicit_authorization(self):
        h, code = held_harness(approve=False)
        assert h.execute('pay', dict(code=code))['reason'] == 'payment_approval_required'
        assert h.airline.calls['pay'] == 0
        assert not h.airline.ledger

    def test_foreign_booking_access_is_denied_for_every_operation(self):
        h, code = held_harness()
        h.airline.bookings[code]['request_id'] = 'other-request'
        for tool in ['pay', 'get_booking', 'cancel_booking']:
            assert h.execute(tool, dict(code=code))['reason'] == 'booking_scope_mismatch'
        assert h.airline.calls['pay'] == 0

    def test_tool_allowlist_and_schema(self):
        h = build_agent(CASES['normal'], 'react').harness
        assert h.execute('transfer_money', dict(amount=10))['reason'] == 'tool_not_allowed'
        assert h.execute('pay', dict(code='X', payment_approved=True))['reason'] == 'invalid_arguments'
        assert h.execute('book_seat', dict(flight_id='VN122', quoted_total=True))['reason'] == 'invalid_arguments'
        assert h.execute('search_flights', dict(origin='HAN', destination='DAD', travel_date='2026-10-07'))['reason'] == 'search_scope_mismatch'

    def test_unobserved_flight_and_forged_price_blocked(self):
        h = build_agent(CASES['normal'], 'react').harness
        assert h.execute('book_seat', dict(flight_id='VN122', quoted_total=1))['reason'] == 'unobserved_flight'
        h.execute('search_flights', h.constraints.search_args())
        assert h.execute('book_seat', dict(flight_id='VN122', quoted_total=1))['reason'] == 'quote_mismatch'
        assert h.airline.calls['book_seat'] == 0

    def test_payment_limit_is_distinct_from_requested_budget(self):
        h, code = held_harness()
        h.policy = h.policy.model_copy(update=dict(approved_total=100))
        assert h.execute('pay', dict(code=code))['reason'] == 'payment_limit_exceeded'
        assert not h.airline.ledger

    def test_policy_scope_cannot_be_reused_for_other_request(self):
        h = build_agent(CASES['normal'], 'react').harness
        h.policy = h.policy.model_copy(update=dict(request_id='wrong'))
        assert h.execute('search_flights', h.constraints.search_args())['reason'] == 'authorization_scope_mismatch'

    def test_paid_not_enough_without_ticket_and_ledger(self):
        h, code = held_harness()
        h.airline.bookings[code].update(paid=True, status='confirmed')
        assert not h.verify()['done']
        assert h.finalize()['status'] == 'HANDOFF'

    def test_ledger_mismatch_fails_verification(self):
        h, code = held_harness()
        h.execute('pay', dict(code=code))
        h.airline.ledger[code]['amount'] += 1
        assert not h.verify()['done']

    def test_paid_ticket_is_not_cancelled_after_readback_failure(self):
        agent = build_agent(CASES['readback_down'], 'react')
        result = agent.run()
        assert result['status'] == 'HANDOFF'
        assert result['reason'] == 'verification_unavailable'
        assert agent.harness.airline.calls['cancel_booking'] == 0
        assert result['handoff']['charges']

    def test_unpaid_hold_is_cleaned_up(self):
        agent = build_agent(CASES['no_authorization'], 'react')
        agent.run()
        assert all(b['status'] == 'cancelled' for b in agent.harness.airline.bookings.values())
        assert agent.harness.airline.flights['VN122']['seats'] == 5

    def test_payment_and_booking_are_idempotent(self):
        h, code = held_harness()
        b = h.airline.bookings[code]
        initial_seats = h.airline.flights[b['flight_id']]['seats']
        repeated = h.execute('book_seat', dict(flight_id=b['flight_id'], quoted_total=b['total']))
        assert repeated['booking']['code'] == code
        assert h.airline.flights[b['flight_id']]['seats'] == initial_seats
        h.execute('pay', dict(code=code))
        h.execute('pay', dict(code=code))
        assert len(h.airline.ledger) == 1
        assert h.verify()['done']
        assert h.execute('cancel_booking', dict(code=code))['reason'] == 'paid_booking_requires_human'

    def test_concurrent_retries_create_one_charge(self):
        h, code = held_harness()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: h.airline.pay(code), range(20)))
        assert all(r['status'] == 'ok' for r in results)
        assert len(h.airline.ledger) == 1

    def test_every_flight_constraint(self):
        for field, value in [
    ('origin', 'HAN'), ('destination', 'CXR'), ('departure', '2026-10-08T08:00:00+07:00'),
    ('departure', '2026-10-07T12:00:00+07:00'), ('departure', '2026-10-07T08:00:00'),
    ('departure', 'bad'), ('price_per_person', 2_000_001), ('price_per_person', -1),
    ('price_per_person', True), ('stops', 1), ('baggage_kg', 19), ('seats', 0),
    ('currency', 'USD'), ('cabin', 'business'),
]:
            with self.subTest(field=field, value=value):
                h = build_agent(CASES['normal'], 'react').harness
                flight = dict(h.airline.flights['VN122'])
                flight[field] = value
                assert h.constraints.violations(flight)

    def test_timezone_is_converted_before_day_and_time_comparison(self):
        h = build_agent(CASES['normal'], 'react').harness
        flight = dict(h.airline.flights['VN122'])
        flight['departure'] = '2026-10-07T01:10:00+00:00'
        assert not h.constraints.violations(flight)

    def test_invalid_constraints_rejected(self):
        for values in [dict(origin='SGN', destination='SGN'), dict(max_total=-1),
                                    dict(passengers=0), dict(depart_after='13:00', depart_before='12:00')]:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    Constraints(**values)

    def test_past_date_is_blocked(self):
        scenario = Scenario(name='past', description='past', constraints=Constraints(travel_date=date(2026, 10, 5)))
        result = build_agent(scenario, 'react').run()
        assert result['reason'] == 'past_travel_date'

    def test_frozen_constraints(self):
        with self.assertRaises(ValidationError):
            Constraints().max_total = 999999999

    def test_budget_enforcement(self):
        for limits, reason in [(Limits(model_calls=1), 'model_budget'),
                                          (Limits(tool_calls=1), 'tool_budget'),
                                          (Limits(input_characters=100), 'context_budget')]:
            with self.subTest(limits=limits, reason=reason):
                result = build_agent(CASES['normal'], 'react', limits=limits).run()
                assert result['status'] == 'HANDOFF'
                assert result['reason'] == reason

    def test_time_budget_enforcement(self):
        agent = build_agent(CASES['normal'], 'react')
        agent.harness.started -= 61
        assert agent.run()['reason'] == 'time_budget'

    def test_replan_limit(self):
        result = build_agent(CASES['price_jump'], 'hybrid', limits=Limits(max_replans=0)).run()
        assert result['status'] == 'HANDOFF'
        assert result['reason'] == 'replan_budget'

    def test_human_plan_rejection_has_no_write_side_effects(self):
        for pattern in ['plan', 'hybrid']:
            with self.subTest(pattern=pattern):
                agent = build_agent(CASES['normal'], pattern, reviewer=lambda _: False)
                result = agent.run()
                assert result['reason'] == 'plan_rejected'
                assert not agent.harness.airline.bookings

    def test_architectures_have_distinct_model_call_patterns(self):
        counts = {p: build_agent(CASES['normal'], p).run()['metrics']['model_calls'] for p in PATTERNS}
        assert counts == dict(react=5, plan=1, hybrid=1)
        adaptive = build_agent(CASES['price_jump'], 'hybrid').run()
        assert adaptive['status'] == 'DONE'
        assert adaptive['metrics']['replans'] >= 1
        assert len(adaptive['plans']) >= 2

    def test_empty_response_is_not_an_empty_flight_list(self):
        result = build_agent(CASES['malformed_search'], 'react').run()
        first = result['trace'][0]['result']
        assert first['status'] == 'error'
        assert first['reason'] == 'invalid_tool_response'
        assert 'flights' not in first

    def test_finalization_is_idempotent(self):
        h, code = held_harness()
        h.execute('pay', dict(code=code))
        first = h.finalize()
        calls = len(h.audit)
        assert h.finalize() == first
        assert len(h.audit) == calls

    def test_corrupted_search_payload_is_rejected(self):
        for corruption in [dict(flight_id=[]), dict(price_per_person=True), dict(departure='2026-10-07Tbroken'), dict(seats='five')]:
            with self.subTest(corruption=corruption):
                agent = build_agent(CASES['normal'], 'react')
                agent.harness.airline.flights['VN122'].update(corruption)
                result = agent.run()
                assert result['status'] == 'HANDOFF'
                assert result['trace'][0]['result']['reason'] == 'invalid_tool_response'
                assert not agent.harness.airline.ledger

    def test_malformed_model_output_is_audited(self):
        agent = build_agent(CASES['normal'], 'react', engine=scripted_engine(behavior='malformed_model'))
        result = agent.run()
        assert len(result['model_trace']) == 1
        assert result['metrics']['input_tokens'] is None


if __name__ == '__main__':
    unittest.main()
