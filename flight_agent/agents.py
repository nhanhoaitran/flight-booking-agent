from __future__ import annotations

from copy import deepcopy
from typing import Callable, TypedDict

from flight_agent.decision_models import DecisionEngine
from flight_agent.domain import Action, PATTERNS, PermissionPolicy, Scenario, Limits
from flight_agent.harness import BookingHarness, HarnessStop
from flight_agent.mock_airline import MockAirline


class AgentState(TypedDict, total=False):
    action: dict
    plan: list[dict]
    index: int
    route: str
    reason: str
    result: dict
    last_result: dict
    action_source: str


class FlightAgent:
    def __init__(self, pattern: str, harness: BookingHarness, engine: DecisionEngine | None = None,
                 reviewer: Callable | None = None):
        if pattern not in PATTERNS:
            raise ValueError(f'Unknown pattern: {pattern}')
        self.pattern = pattern
        self.harness = harness
        self.engine = engine or DecisionEngine.live()
        self.reviewer = reviewer
        self.graph = self._build_graph()

    def _build_graph(self):
        raise NotImplementedError

    def bootstrap(self, state):
        result = self.harness.execute('search_flights', self.harness.constraints.search_args())
        if result['status'] == 'ok':
            return dict(route='plan')
        if self.pattern == 'hybrid' and result.get('retryable'):
            return dict(route='react')
        return dict(route='finalize', reason=result.get('reason', 'search_failed'))

    def plan(self, state):
        h = self.harness
        if h.plan_audit:
            if h.replans >= h.limits.max_replans:
                return dict(route='finalize', reason='replan_budget')
            h.replans += 1
        proposal = self.engine.decide(h, 'plan')
        approved = h.policy.plan_approved
        if self.reviewer is not None:
            approved = approved and bool(self.reviewer(proposal.model_dump(mode='json')))
        h.plan_audit.append(dict(plan=proposal.model_dump(mode='json'), approved=approved))
        if not approved:
            return dict(route='finalize', reason='plan_rejected')
        if not proposal.steps:
            return dict(route='finalize', reason='empty_plan')
        return dict(plan=[step.model_dump(mode='json') for step in proposal.steps], index=0, route='next_step')

    def next_step(self, state):
        if state['index'] >= len(state['plan']):
            return dict(route='finalize', reason='plan_exhausted')
        action = deepcopy(state['plan'][state['index']])
        for key, value in action['args'].items():
            if isinstance(value, str) and value.startswith('$quoted_total:'):
                flight_id = value.split(':', 1)[1]
                quote = self.harness.checked_quotes.get(flight_id)
                if quote is None:
                    return dict(route='finalize', reason='unresolved_quote_reference')
                action['args'][key] = quote
            if value == '$booking_code':
                booking = self.harness.observed_booking
                if not booking:
                    return dict(route='finalize', reason='unresolved_booking_reference')
                action['args'][key] = booking['code']
        return dict(action=action, action_source='plan', route='execute')

    def react(self, state):
        action = self.engine.decide(self.harness, 'react')
        return dict(action=action.model_dump(mode='json'), action_source='react', route='execute')

    def execute(self, state):
        h = self.harness
        action = Action.model_validate(state['action'])
        if action.tool in ('finish', 'handoff'):
            return dict(route='finalize', reason=action.rationale or 'model_stopped')
        result = h.execute(action.tool, action.args)
        update = dict(last_result=result)
        if h.stop_reason:
            return dict(**update, route='finalize', reason=h.stop_reason)
        if result['status'] != 'ok':
            reason = result.get('reason', 'tool_failed')
            if result['status'] in ('denied', 'needs_approval') or not result.get('retryable'):
                return dict(**update, route='finalize', reason=reason)
            if self.pattern == 'plan':
                return dict(**update, route='finalize', reason=reason)
            return dict(**update, route='react')
        if action.tool == 'get_booking' and result['booking']['paid'] and result['booking']['status'] == 'confirmed':
            return dict(**update, route='finalize', reason='observed_confirmed_booking')
        if self.pattern == 'react':
            return dict(**update, route='react')
        if self.pattern == 'hybrid' and action.tool == 'check_seat':
            flight = result['flight']
            if h.constraints.violations(flight) or h.approval_reason(flight, result['total']):
                return dict(**update, route='react')
        if self.pattern == 'hybrid' and state.get('route') == 'execute':
            if state.get('action_source') == 'react':
                return dict(**update, route='plan')
            if action.tool == 'get_booking' and not result['booking']['paid']:
                return dict(**update, route='plan')
        return dict(**update, index=state.get('index', 0) + 1, route='next_step')

    def finalize(self, state):
        return dict(result=self.harness.finalize(state.get('reason', 'goal_not_reached')))

    def run(self):
        try:
            state = self.graph.invoke(dict(plan=[], index=0, reason=''), config=dict(recursion_limit=200))
            result = state['result']
        except HarnessStop as exc:
            result = self.harness.finalize(str(exc))
        except Exception as exc:
            result = self.harness.finalize(f'agent_error:{type(exc).__name__}')
            result['error_type'] = type(exc).__name__
        result.update(pattern=self.pattern, backend=self.engine.backend, request_id=self.harness.airline.request_id)
        return result


def build_agent(scenario: Scenario, pattern: str, seed=0, engine=None, limits=None,
                approve_payment=None, reviewer=None, request_id=None, risk_approved=False, reference_date=None):
    from flight_agent.react import ReActAgent
    from flight_agent.plan_execute import PlanExecuteAgent
    from flight_agent.hybrid import HybridAgent

    run_id = request_id or f'{scenario.name}-{seed}'
    airline = MockAirline(scenario, run_id, seed)
    approved = scenario.approve_payment if approve_payment is None else approve_payment
    policy = PermissionPolicy(payment_approved=approved,
                              approved_total=scenario.constraints.max_total if approved else 0,
                              subject=scenario.constraints.passenger_id, request_id=run_id,
                              auto_approve_limit=scenario.auto_approve_limit, risk_approved=risk_approved)
    harness = BookingHarness(airline, policy, limits or Limits(), reference_date=reference_date)
    classes = {'react': ReActAgent, 'plan': PlanExecuteAgent, 'hybrid': HybridAgent}
    if pattern not in classes:
        raise ValueError(f'Unknown pattern: {pattern}')
    return classes[pattern](pattern, harness, engine, reviewer)
