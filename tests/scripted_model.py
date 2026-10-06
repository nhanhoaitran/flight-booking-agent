import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from flight_agent.domain import Action, Constraints, Plan


def choose_action(context):
    c = Constraints.model_validate(context['constraints'])
    last = context.get('last')
    result = last['result'] if last else {}
    if result.get('status') in ('denied', 'needs_approval') or (result.get('status') == 'error' and not result.get('retryable')):
        return Action(tool='handoff', rationale=result.get('reason', 'tool_failed'))
    booking = context.get('booking')
    if booking and booking['status'] != 'cancelled':
        code = booking['code']
        if result.get('status') == 'unknown' or (last and last['tool'] == 'get_booking' and result.get('status') == 'error'):
            return Action(tool='get_booking', args=dict(code=code), rationale='Đối soát trạng thái trước khi tiếp tục')
        if booking['paid']:
            if last and last['tool'] == 'get_booking' and result.get('status') == 'ok':
                return Action(tool='finish', rationale='Đã đọc lại vé; harness kiểm điều kiện hoàn thành')
            return Action(tool='get_booking', args=dict(code=code), rationale='Xác minh vé sau thanh toán')
        return Action(tool='pay', args=dict(code=code), rationale='Thanh toán giữ chỗ đúng yêu cầu qua kiểm quyền')
    if not context['searched']:
        return Action(tool='search_flights', args=c.search_args(), rationale='Lấy dữ liệu chuyến bay từ tool')
    valid = [f for f in context['flights'] if not c.violations(f)]
    if not valid:
        return Action(tool='handoff', rationale='no_valid_flight')
    best = min(valid, key=lambda f: (f['price_per_person'], f['departure'], f['flight_id']))
    if best['flight_id'] not in context.get('checked_quotes', {}):
        return Action(tool='check_seat', args=dict(flight_id=best['flight_id']))
    return Action(tool='book_seat', args=dict(flight_id=best['flight_id'],
                  quoted_total=best['price_per_person'] * c.passengers), rationale='Chọn chuyến rẻ nhất đáp ứng đủ ràng buộc')


def remaining_plan(context):
    action = choose_action(context)
    if action.tool in ('handoff', 'finish', 'search_flights'):
        return Plan(steps=[action], rationale='Kế hoạch dựa trên dữ liệu hiện có')
    steps = [action]
    if action.tool == 'check_seat':
        flight_id = action.args['flight_id']
        steps += [Action(tool='book_seat', args=dict(flight_id=flight_id, quoted_total=f'$quoted_total:{flight_id}')),
                  Action(tool='pay', args=dict(code='$booking_code')),
                  Action(tool='get_booking', args=dict(code='$booking_code'))]
    if action.tool == 'book_seat':
        steps += [Action(tool='pay', args=dict(code='$booking_code')),
                  Action(tool='get_booking', args=dict(code='$booking_code'))]
    if action.tool == 'pay':
        steps += [Action(tool='get_booking', args=dict(code=action.args['code']))]
    return Plan(steps=steps, rationale='Giữ chỗ, thanh toán theo quyền và đọc lại vé')


class ScriptedDecisionModel(BaseChatModel):
    behavior: str = 'normal'

    @property
    def _llm_type(self):
        return 'offline-observation-policy'

    @property
    def _identifying_params(self):
        return dict(behavior=self.behavior)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(messages[-1].content)
        context = payload['context']
        action = None
        if self.behavior == 'loop':
            action = Action(tool='search_flights', args=Constraints.model_validate(context['constraints']).search_args())
        elif self.behavior == 'hallucination':
            action = Action(tool='finish', rationale='Đã đặt vé VN999 với mã không tồn tại')
        elif self.behavior == 'drift' and context['searched']:
            flight = next((f for f in context['flights'] if f['flight_id'] == 'VJ604'), context['flights'][0])
            action = Action(tool='book_seat', args=dict(flight_id=flight['flight_id'], quoted_total=flight['price_per_person']))
        elif self.behavior == 'forged_permission' and context['searched']:
            action = Action(tool='pay', args=dict(code='FOREIGN-001', payment_approved=True))
        if self.behavior == 'malformed_model':
            output = '{not valid json'
        elif action is not None:
            output = (Plan(steps=[action]) if payload['mode'] == 'plan' else action).model_dump_json()
        else:
            decision = remaining_plan(context) if payload['mode'] == 'plan' else choose_action(context)
            output = decision.model_dump_json()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=output))])




def scripted_engine(behavior='normal'):
    from flight_agent.decision_models import DecisionEngine

    return DecisionEngine(ScriptedDecisionModel(behavior=behavior), backend='test_fixture')
