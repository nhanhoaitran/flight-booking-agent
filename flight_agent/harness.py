from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import date, datetime
import json
from time import perf_counter

from pydantic import ValidationError

from flight_agent.domain import BookingRecord, Constraints, Flight, Limits, PermissionPolicy, LOCAL_TZ
from flight_agent.mock_airline import MockAirline


class HarnessStop(RuntimeError):
    pass


class BookingHarness:
    def __init__(self, airline: MockAirline, policy: PermissionPolicy, limits: Limits | None = None,
                 reference_date: date | None = None):
        self.airline = airline
        self.constraints: Constraints = airline.constraints
        self.policy = policy
        self.limits = limits or Limits()
        self.reference_date = reference_date or datetime.now(LOCAL_TZ).date()
        self.registry = airline.tools()
        self.started = perf_counter()
        self.audit = []
        self.model_audit = []
        self.plan_audit = []
        self.observed_flights = {}
        self.checked_quotes = {}
        self.booking_quotes = {}
        self.pending_payments = set()
        self.recent_observations = deque(maxlen=8)
        self.observed_booking = None
        self.milestones = set()
        self.stall = 0
        self.model_calls = 0
        self.input_characters = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.token_usage_complete = True
        self.stop_reason = ''
        self.replans = 0
        self.finalized = None

    def _budget(self, kind):
        if perf_counter() - self.started >= self.limits.wall_seconds:
            raise HarnessStop('time_budget')
        if kind == 'model' and self.model_calls >= self.limits.model_calls:
            raise HarnessStop('model_budget')
        if kind == 'tool' and self.agent_tool_calls >= self.limits.tool_calls:
            raise HarnessStop('tool_budget')
        if self.input_characters >= self.limits.input_characters:
            raise HarnessStop('context_budget')

    @property
    def agent_tool_calls(self):
        return sum(entry['phase'] == 'agent' for entry in self.audit)

    def before_model(self, context):
        self._budget('model')
        size = len(json.dumps(context, ensure_ascii=False, default=str))
        if self.input_characters + size > self.limits.input_characters:
            raise HarnessStop('context_budget')
        self.model_calls += 1
        self.input_characters += size

    def record_model(self, mode, reply, parsed, duration_ms):
        usage = getattr(reply, 'usage_metadata', None)
        if usage:
            self.input_tokens += usage.get('input_tokens', 0)
            self.output_tokens += usage.get('output_tokens', 0)
        else:
            self.token_usage_complete = False
        self.model_audit.append(dict(mode=mode, output=parsed.model_dump(mode='json') if parsed is not None else None,
                                     duration_ms=duration_ms, usage=usage,
                                     finish_reason=getattr(reply, 'response_metadata', {}).get('finish_reason'),
                                     reported_model=getattr(reply, 'response_metadata', {}).get('model_name')))

    def check_permission(self, name, args):
        c = self.constraints
        if name not in self.policy.allowed_tools or name not in self.registry:
            return 'tool_not_allowed'
        if self.policy.request_id != self.airline.request_id or self.policy.subject != c.passenger_id:
            return 'authorization_scope_mismatch'
        if c.travel_date < self.reference_date:
            return 'past_travel_date'
        if name == 'search_flights':
            return None if args == c.search_args() else 'search_scope_mismatch'
        if name == 'check_seat' and args['flight_id'] not in self.observed_flights:
            return 'unobserved_flight'
        if name == 'book_seat':
            flight = self.observed_flights.get(args['flight_id'])
            if flight is None:
                return 'unobserved_flight'
            if c.violations(flight):
                return 'constraint_violation'
            if args['quoted_total'] != flight['price_per_person'] * c.passengers:
                return 'quote_mismatch'
            if args['flight_id'] not in self.checked_quotes:
                return 'seat_check_required'
            if args['quoted_total'] != self.checked_quotes[args['flight_id']]:
                return 'quote_mismatch'
            approval = self.approval_reason(flight, args['quoted_total'])
            if approval:
                return approval
            active = [b for b in self.airline.bookings.values() if b['request_id'] == self.airline.request_id
                      and b['status'] != 'cancelled']
            if active and any(b['flight_id'] != args['flight_id'] for b in active):
                return 'active_booking_exists'
        if name in ('pay', 'get_booking', 'cancel_booking'):
            booking = self.airline.bookings.get(args['code'])
            if not booking or booking['request_id'] != self.airline.request_id or booking['passenger_id'] != c.passenger_id:
                return 'booking_scope_mismatch'
            if name == 'pay':
                if args['code'] in self.pending_payments:
                    return 'payment_reconciliation_required'
                if not self.policy.payment_approved:
                    return 'payment_approval_required'
                if booking['total'] > self.policy.approved_total:
                    return 'payment_limit_exceeded'
                if booking['status'] not in ('held', 'confirmed'):
                    return 'booking_not_payable'
                if c.violations(booking['flight'], require_seats=False):
                    return 'constraint_violation'
                if booking['flight_id'] != booking['flight']['flight_id']:
                    return 'booking_data_mismatch'
                if booking['passengers'] != c.passengers or booking['currency'] != c.currency:
                    return 'booking_data_mismatch'
                if booking['total'] != booking['flight']['price_per_person'] * c.passengers:
                    return 'booking_data_mismatch'
                if self.booking_quotes.get(booking['code']) != booking['total']:
                    return 'quote_mismatch'
                approval = self.approval_reason(booking['flight'], booking['total'])
                if approval:
                    return approval
            if name == 'cancel_booking' and (booking['paid'] or args['code'] in self.airline.ledger):
                return 'paid_booking_requires_human'
        return None

    def approval_reason(self, flight, total):
        if not self.policy.risk_approved:
            if total > self.policy.auto_approve_limit:
                return 'auto_approval_limit_exceeded'
            if self.policy.require_refundable_approval and not flight.get('refundable', True):
                return 'non_refundable_approval_required'
        return None

    def _normalize(self, name, result):
        if not isinstance(result, dict) or result.get('status') not in ('ok', 'error', 'unknown', 'denied'):
            return dict(status='error', reason='invalid_tool_response', retryable=True)
        if result['status'] == 'ok':
            if name == 'check_seat':
                try:
                    flight = Flight.model_validate(result.get('flight'))
                    if type(result.get('total')) is not int or result['total'] != flight.price_per_person * self.constraints.passengers:
                        raise ValueError('Invalid quote')
                except (ValidationError, ValueError):
                    return dict(status='error', reason='invalid_tool_response', retryable=True)
                return result
            if name == 'search_flights':
                rows = result.get('flights')
                required = {'flight_id', 'origin', 'destination', 'departure', 'price_per_person',
                            'currency', 'baggage_kg', 'stops', 'cabin', 'seats'}
                if not isinstance(rows, list) or any(not isinstance(f, dict) or not required <= f.keys() for f in rows):
                    return dict(status='error', reason='invalid_tool_response', retryable=True)
                try:
                    for row in rows:
                        Flight.model_validate(row)
                except ValidationError:
                    return dict(status='error', reason='invalid_tool_response', retryable=True)
                if len({f['flight_id'] for f in rows}) != len(rows):
                    return dict(status='error', reason='duplicate_flight_identifiers', retryable=False)
            elif not isinstance(result.get('booking'), dict) or not {'code', 'status', 'paid', 'flight', 'total'} <= result['booking'].keys():
                return dict(status='error', reason='invalid_tool_response', retryable=True)
            if name != 'search_flights':
                try:
                    BookingRecord.model_validate(result['booking'])
                except ValidationError:
                    return dict(status='error', reason='invalid_tool_response', retryable=True)
        return result

    def _record(self, name, args, result, phase, duration):
        entry = dict(index=len(self.audit) + 1, phase=phase, tool=name, args=deepcopy(args),
                     result=deepcopy(result), duration_ms=round(duration * 1000, 4))
        self.audit.append(entry)
        if name == 'pay' and result['status'] == 'unknown':
            self.pending_payments.add(args['code'])
        if name == 'get_booking' and result['status'] == 'ok':
            self.pending_payments.discard(args['code'])
        if result['status'] == 'ok':
            if name == 'search_flights':
                self.observed_flights = {f['flight_id']: deepcopy(f) for f in result['flights']}
            if name == 'check_seat':
                flight = result['flight']
                self.checked_quotes[flight['flight_id']] = result['total']
                self.observed_flights[flight['flight_id']] = deepcopy(flight)
            if name == 'book_seat':
                self.booking_quotes.setdefault(result['booking']['code'], self.checked_quotes.get(args['flight_id']))
            if 'booking' in result:
                self.observed_booking = deepcopy(result['booking'])
        elif isinstance(result.get('flight'), dict):
            try:
                flight = Flight.model_validate(result['flight']).model_dump()
                self.observed_flights[flight['flight_id']] = flight
                self.checked_quotes.pop(flight['flight_id'], None)
            except ValidationError:
                pass
        if phase == 'agent':
            fingerprint = json.dumps([name, args, result], sort_keys=True, default=str)
            self.recent_observations.append(fingerprint)
            milestones = self._progress()
            if milestones - self.milestones:
                self.stall = 0
                self.milestones |= milestones
            else:
                self.stall += 1
            if name != 'get_booking' and self.recent_observations.count(fingerprint) >= self.limits.repeated_failures:
                self.stop_reason = 'loop_detected'
            elif self.stall >= self.limits.stalled_steps:
                self.stop_reason = 'no_progress'
        return deepcopy(result)

    def _progress(self):
        result = {f'flight:{key}:{row["price_per_person"]}:{row["seats"]}' for key, row in self.observed_flights.items()}
        result.update(f'checked:{flight_id}:{total}' for flight_id, total in self.checked_quotes.items())
        if self.observed_booking:
            b = self.observed_booking
            result.add(f'booking:{b["code"]}:{b["status"]}:{b["paid"]}')
        return result

    def execute(self, name: str, args: dict, phase='agent'):
        if phase == 'agent':
            if self.stop_reason:
                raise HarnessStop(self.stop_reason)
            self._budget('tool')
        started = perf_counter()
        if name not in self.registry:
            return self._record(name, args, dict(status='denied', reason='tool_not_allowed', retryable=False), phase, 0)
        try:
            validated = self.registry[name].args_schema.model_validate(args).model_dump()
        except ValidationError:
            return self._record(name, args, dict(status='denied', reason='invalid_arguments', retryable=False), phase, 0)
        reason = self.check_permission(name, validated)
        if reason:
            approval_reasons = {'payment_approval_required', 'payment_limit_exceeded',
                                'auto_approval_limit_exceeded', 'non_refundable_approval_required'}
            result = dict(status='needs_approval' if reason in approval_reasons else 'denied',
                          reason=reason, retryable=False)
        else:
            try:
                result = self._normalize(name, self.registry[name].invoke(validated))
                if result['status'] == 'ok':
                    wrong_flight = name == 'check_seat' and result['flight']['flight_id'] != validated['flight_id']
                    wrong_code = name in ('pay', 'get_booking', 'cancel_booking') and result['booking']['code'] != validated['code']
                    wrong_booking = name == 'book_seat' and result['booking']['flight_id'] != validated['flight_id']
                    if wrong_flight or wrong_code or wrong_booking:
                        result = dict(status='error', reason='tool_identity_mismatch', retryable=False)
            except Exception as exc:
                result = dict(status='error', reason='tool_exception', error_type=type(exc).__name__, retryable=False)
        return self._record(name, validated, result, phase, perf_counter() - started)

    def context(self):
        last = next((entry for entry in reversed(self.audit) if entry['phase'] == 'agent'), None)
        return dict(constraints=self.constraints.model_dump(mode='json'),
                    policy=self.policy.model_dump(mode='json'), checked_quotes=deepcopy(self.checked_quotes),
                    flights=deepcopy(list(self.observed_flights.values())),
                    booking=deepcopy(self.observed_booking), last=deepcopy(last),
                    searched=any(e['tool'] == 'search_flights' and e['result']['status'] == 'ok' for e in self.audit),
                    observations=deepcopy(self.audit[-10:]),
                    tool_schemas=[dict(name=t.name, description=t.description,
                                      parameters=t.args_schema.model_json_schema()) for t in self.registry.values()])

    def verify(self):
        candidates = [b for b in self.airline.bookings.values() if b['request_id'] == self.airline.request_id
                      and b['status'] != 'cancelled']
        if len(candidates) != 1:
            return dict(done=False, reason='no_unique_booking', booking=None)
        readback = self.execute('get_booking', dict(code=candidates[0]['code']), phase='verification')
        if readback['status'] != 'ok':
            return dict(done=False, reason='verification_unavailable', booking=None)
        b = readback['booking']
        ledger = self.airline.ledger.get(b['code'])
        owned_charges = [v for v in self.airline.ledger.values() if v['request_id'] == self.airline.request_id]
        checks = {
            'confirmed': b['status'] == 'confirmed', 'paid': b['paid'] is True,
            'ticket': isinstance(b.get('ticket'), str) and bool(b['ticket']),
            'scope': b['request_id'] == self.policy.request_id and b['passenger_id'] == self.policy.subject,
            'constraints': not self.constraints.violations(b['flight'], require_seats=False),
            'passengers': b['passengers'] == self.constraints.passengers,
            'currency': b['currency'] == self.constraints.currency,
            'total': b['total'] == b['flight']['price_per_person'] * self.constraints.passengers,
            'checked_quote': self.booking_quotes.get(b['code']) == b['total'],
            'risk_approval': self.approval_reason(b['flight'], b['total']) is None,
            'flight_identity': b['flight_id'] == b['flight']['flight_id'],
            'authorization': self.policy.payment_approved and b['total'] <= self.policy.approved_total,
            'ledger': bool(ledger) and ledger['amount'] == b['total'] and ledger['currency'] == b['currency']
                      and ledger['passenger_id'] == self.policy.subject and ledger['request_id'] == self.policy.request_id
                      and ledger['code'] == b['code'] and len(owned_charges) == 1,
        }
        return dict(done=all(checks.values()), reason='verified' if all(checks.values()) else 'completion_checks_failed',
                    checks=checks, booking=b)

    def handoff(self, reason, verification):
        questions = {
            'payment_approval_required': 'Bạn có cho phép thanh toán tối đa ngân sách đã yêu cầu cho đúng hành khách và hành trình này không?',
            'payment_limit_exceeded': 'Hạn mức được duyệt thấp hơn giá vé; bạn có muốn tăng hạn mức cho giao dịch này không?',
            'no_valid_flight': 'Không có chuyến đáp ứng đủ ràng buộc; bạn muốn đổi ngày, khung giờ hay ngân sách?',
            'payment_declined': 'Thanh toán bị từ chối; bạn muốn dùng phương thức thanh toán khác không?',
            'verification_unavailable': 'Trạng thái vé chưa xác minh được; nhân viên có thể đối soát mã đặt chỗ và giao dịch trước khi thử lại không?',
            'auto_approval_limit_exceeded': 'Giá vé vượt hạn mức tự duyệt; người có thẩm quyền có duyệt giao dịch này không?',
            'non_refundable_approval_required': 'Vé không hoàn; bạn có chấp nhận điều kiện này trước khi giữ chỗ không?',
            'plan_rejected': 'Bạn muốn sửa bước nào trong kế hoạch trước khi cho phép thực thi?',
        }
        searched = any(e['tool'] == 'search_flights' and e['result']['status'] == 'ok' for e in self.audit)
        progress = sum([searched, bool(self.checked_quotes),
                        bool(self.airline.bookings), bool(self.airline.ledger)])
        return dict(stop_reason=reason, progress=f'{progress}/4', plans=deepcopy(self.plan_audit),
                    policy=self.policy.model_dump(mode='json'), constraints=self.constraints.model_dump(mode='json'),
                    done_so_far=deepcopy(list(self.airline.bookings.values())),
                    charges=deepcopy(list(self.airline.ledger.values())),
                    tried=deepcopy(self.audit), verification=verification,
                    question=questions.get(reason, f'Quy trình dừng vì {reason}; bạn muốn nhân viên kiểm tra và tiếp tục yêu cầu này không?'),
                    next_owner='human_operator', request_id=self.airline.request_id)

    def finalize(self, reason='goal_not_reached'):
        if self.finalized is not None:
            return deepcopy(self.finalized)
        verification = self.verify()
        status = 'DONE' if verification['done'] else 'HANDOFF'
        if status == 'HANDOFF':
            if verification['reason'] == 'verification_unavailable':
                reason = verification['reason']
            for booking in list(self.airline.bookings.values()):
                if booking['request_id'] == self.airline.request_id and booking['status'] == 'held':
                    read = self.execute('get_booking', dict(code=booking['code']), phase='cleanup')
                    if read['status'] == 'ok' and not read['booking']['paid']:
                        self.execute('cancel_booking', dict(code=booking['code']), phase='cleanup')
        self.finalized = dict(status=status, reason='verified' if status == 'DONE' else reason,
                              booking=verification['booking'] if status == 'DONE' else None,
                              verification=verification,
                              handoff=None if status == 'DONE' else self.handoff(reason, verification),
                              metrics=self.metrics(), trace=deepcopy(self.audit),
                              model_trace=deepcopy(self.model_audit), plans=deepcopy(self.plan_audit))
        return deepcopy(self.finalized)

    def metrics(self):
        return dict(model_calls=self.model_calls, agent_tool_calls=self.agent_tool_calls,
                    total_tool_calls=len(self.audit), denied_calls=sum(e['result']['status'] == 'denied' for e in self.audit),
                    replans=self.replans, input_characters=self.input_characters,
                    input_tokens=self.input_tokens if self.token_usage_complete else None,
                    output_tokens=self.output_tokens if self.token_usage_complete else None,
                    latency_ms=round((perf_counter() - self.started) * 1000, 4),
                    charged_total=sum(x['amount'] for x in self.airline.ledger.values()),
                    charge_count=len(self.airline.ledger))
