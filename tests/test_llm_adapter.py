import json
import os
import unittest
from unittest.mock import patch

import httpx
from langchain_openai import ChatOpenAI

from flight_agent.agents import build_agent
from flight_agent.decision_models import DecisionEngine
from flight_agent.domain import PATTERNS, scenarios
from tests.scripted_model import choose_action, remaining_plan


class LLMAdapterTests(unittest.TestCase):
    def test_real_adapter_with_mock_http_completions(self):
        cases = {case.name: case for case in scenarios()}
        settings = dict(OPENAI_API_KEY='test-key-never-sent', OPENAI_MODEL='test-model',
                        OPENAI_BASE_URL='https://example.test/v1', LANGSMITH_TRACING='false')
        for case_name in ('normal', 'price_jump'):
            for pattern in PATTERNS:
                with self.subTest(case=case_name, pattern=pattern):
                    requests = []

                    def respond(request):
                        payload = json.loads(request.content)
                        self.assertEqual(request.headers['authorization'], 'Bearer test-key-never-sent')
                        self.assertNotIn('test-key-never-sent', json.dumps(payload))
                        self.assertEqual(payload['model'], 'test-model')
                        decision = json.loads(payload['messages'][-1]['content'])
                        context = decision['context']
                        action = remaining_plan(context) if decision['mode'] == 'plan' else choose_action(context)
                        requests.append(payload)
                        return httpx.Response(200, json=dict(
                            id='mock-completion', object='chat.completion', created=0, model='test-model',
                            choices=[dict(index=0, finish_reason='stop',
                                          message=dict(role='assistant', content=action.model_dump_json()))],
                            usage=dict(prompt_tokens=7, completion_tokens=3, total_tokens=10)))

                    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                        with patch.dict(os.environ, settings, clear=True):
                            with patch('langchain_openai.ChatOpenAI', side_effect=lambda **kwargs: ChatOpenAI(**kwargs, http_client=client)):
                                engine = DecisionEngine.live()
                            case = cases[case_name]
                            result = build_agent(case, pattern, engine=engine, reference_date=case.reference_date).run()
                    expected = 'HANDOFF' if case_name == 'price_jump' and pattern == 'plan' else 'DONE'
                    self.assertEqual(result['status'], expected)
                    self.assertEqual(result['metrics']['input_tokens'], len(requests) * 7)
                    self.assertEqual(result['metrics']['output_tokens'], len(requests) * 3)
                    self.assertNotIn(settings['OPENAI_API_KEY'], json.dumps(result))

    def test_http_authentication_failure_is_audited_without_secret(self):
        secret = 'test-key-never-sent'
        settings = dict(OPENAI_API_KEY=secret, OPENAI_MODEL='test-model',
                        OPENAI_BASE_URL='https://example.test/v1', LANGSMITH_TRACING='false')

        def reject(request):
            return httpx.Response(401, json=dict(error=dict(message=secret, type='authentication_error')))

        with httpx.Client(transport=httpx.MockTransport(reject)) as client:
            with patch.dict(os.environ, settings, clear=True):
                with patch('langchain_openai.ChatOpenAI', side_effect=lambda **kwargs: ChatOpenAI(**kwargs, http_client=client)):
                    engine = DecisionEngine.live()
                case = next(case for case in scenarios() if case.name == 'normal')
                agent = build_agent(case, 'react', engine=engine, reference_date=case.reference_date)
                result = agent.run()
        self.assertEqual(result['status'], 'HANDOFF')
        self.assertIn('Authentication', result['reason'])
        self.assertFalse(agent.harness.airline.ledger)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIsNone(result['metrics']['input_tokens'])


if __name__ == '__main__':
    unittest.main()
