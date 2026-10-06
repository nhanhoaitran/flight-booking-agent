import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from flight_agent.agents import build_agent
from flight_agent.decision_models import DecisionEngine
from flight_agent.domain import Constraints, PATTERNS, scenarios
from flight_agent.evaluation import ARTIFACTS, evaluate
from flight_agent.reporting import format_result, write_run


def plan_review(plan):
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return input('Duyệt kế hoạch này? [y/N]: ').strip().lower() == 'y'


def parser():
    root = argparse.ArgumentParser(description='SE373 BTVN03: agent đặt vé dùng LLM và tool mock')
    commands = root.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo', help='Chạy một yêu cầu bằng LLM thật')
    demo.add_argument('--pattern', choices=PATTERNS, default='react')
    demo.add_argument('--scenario', choices=[s.name for s in scenarios()], default='normal')
    demo.add_argument('--seed', type=int, default=0)
    demo.add_argument('--approve-payment', action='store_true', help='Cấp quyền thanh toán giả lập trong ngân sách')
    demo.add_argument('--approve-risk', action='store_true', help='Duyệt ngoại lệ hạn mức tự duyệt/vé không hoàn')
    demo.add_argument('--review-plan', action='store_true', help='Duyệt từng kế hoạch trước thực thi')
    demo.add_argument('--request', type=Path, help='Đọc ràng buộc từ JSON do người dùng cung cấp')
    demo.add_argument('--model', help='Ghi đè OPENAI_MODEL; không đặt API key trên dòng lệnh')
    demo.add_argument('--trace', action='store_true', help='In nhật ký đầy đủ ngoài báo cáo Markdown')
    demo.add_argument('--benchmark-clock', action='store_true', help='Dùng mốc giả lập 2026-10-06 của bộ kịch bản')
    batch = commands.add_parser('evaluate', help='Đánh giá LLM thật trên cả ba mẫu')
    batch.add_argument('--repeats', type=int, default=3)
    batch.add_argument('--scenarios', nargs='+', choices=[s.name for s in scenarios()])
    batch.add_argument('--model')
    batch.add_argument('--output', type=Path, help='Đường dẫn báo cáo .md trong Bao_Cao_Ket_Qua')
    batch.add_argument('--trace', action='store_true', help='Lưu toàn bộ trace trong Markdown')
    commands.add_parser('scenarios', help='Liệt kê kịch bản')
    return root


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args()
    if args.command == 'scenarios':
        for scenario in scenarios():
            print(f'{scenario.name}: {scenario.description}')
        return 0
    if args.command == 'demo':
        scenario = next(s for s in scenarios() if s.name == args.scenario)
        if args.request:
            request = Constraints.model_validate_json(args.request.read_text(encoding='utf-8-sig'))
            scenario = scenario.model_copy(update=dict(constraints=request))
        engine = DecisionEngine.live(args.model)
        agent = build_agent(scenario, args.pattern, seed=args.seed, engine=engine,
                            approve_payment=args.approve_payment, risk_approved=args.approve_risk,
                            reference_date=scenario.reference_date if args.benchmark_clock else None,
                            reviewer=plan_review if args.review_plan else None)
        result = agent.run()
        target = ARTIFACTS / f'lan_chay_demo_{args.pattern}_{datetime.now().strftime("%Y%m%d_%H%M%S_%f")}.md'
        write_run(result, target)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.trace else format_result(result))
        print(f'Báo cáo: {target}')
        return 0 if result['status'] == 'DONE' else 2
    if args.output and (args.output.suffix.lower() != '.md'
                        or not args.output.resolve().is_relative_to(ARTIFACTS.resolve())):
        raise ValueError('--output phải là file .md trong Bao_Cao_Ket_Qua.')
    artifact = evaluate(args.repeats, args.model, case_names=args.scenarios, include_trace=args.trace, output=args.output,
                        progress=lambda n, total: print(f'Đã đánh giá {n}/{total} lượt', flush=True))
    print(f"Báo cáo: {artifact['report_path']}")
    if not artifact['metadata']['completed']:
        print('Đợt đánh giá dừng do lỗi provider liên tiếp; xem báo cáo, kiểm tra kết nối trước khi chạy lại.')
    return 2 if any(row['technical_failure'] for row in artifact['rows']) else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        if isinstance(exc, ValueError) and type(exc) is ValueError:
            print(f'Lỗi cấu hình: {exc}', file=sys.stderr)
        else:
            print(f'Lỗi cấu hình: {type(exc).__name__}. Kiểm tra dữ liệu đầu vào và requirements.txt.', file=sys.stderr)
        raise SystemExit(1)
