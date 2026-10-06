import json
from pathlib import Path


def display(value, percent=False):
    if value is None:
        return 'N/A'
    if percent:
        return f'{value * 100:.1f}%'
    return f'{value:.2f}' if isinstance(value, float) else str(value)


def write_run(result, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = f"# Nhật ký chạy {result['pattern']}\n\nBackend: `{result['backend']}`. Kết quả: **{result['status']}**.\n\n"
    text += '```json\n' + json.dumps(result, ensure_ascii=False, indent=2) + '\n```\n'
    target.write_text(text, encoding='utf-8')


def write_evaluation(artifact, target, include_trace=False):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = artifact['metadata']
    lines = ['# Kết quả đánh giá ba mẫu agent', '',
             f"Thời điểm UTC: {meta['created_at']}. Model: `{meta['model']}`. Backend: `{meta['backend']}`.", '',
             f"Thiết kế: {meta['scenario_count']} kịch bản × {meta['seed_count']} lần × 3 mẫu = {meta.get('planned_run_count', meta['run_count'])} lượt dự kiến. Đã thực hiện: {meta['run_count']} lượt.", '',
             'Mỗi lượt dùng inventory, ràng buộc và chính sách riêng. Thanh toán hoàn toàn giả lập.', '']
    if meta['backend'] != 'llm':
        lines += ['**Đây là kiểm thử điều phối bằng model kịch bản, không phải kết quả LLM thật và không đo chất lượng suy luận.**', '']
    if meta.get('stop_reason'):
        lines += [f"**Đợt đánh giá chưa hoàn tất: {meta['stop_reason']}. Không xếp hạng ba mẫu từ đợt chạy thiếu này.**", '']
    if meta.get('model_responses') == 0:
        lines += ['**Chưa nhận phản hồi model. Các số dưới đây chỉ mô tả lần thử chạy; không phải kết quả đánh giá năng lực LLM.**', '']
    failures = sum(row['technical_failure'] for row in artifact['rows'])
    if failures:
        lines += [f'**Có {failures}/{meta["run_count"]} lượt lỗi kỹ thuật. Các lượt này không được dùng để kết luận chất lượng suy luận; an toàn khi chưa thực thi thanh toán không chứng minh model xử lý đúng.**', '']
    lines += ['## Tổng hợp', '', '| Chỉ số | ReAct | Plan-then-Execute | Lai |', '|---|---:|---:|---:|']
    metrics = [('Số lượt', 'runs', False), ('Lượt lỗi kỹ thuật', 'technical_failure_count', False), ('Đúng kết cục kỳ vọng', 'outcome_accuracy', True),
               ('Đặt thành công / ca khả thi', 'booking_success', True),
               ('Phục hồi / ca có thể phục hồi', 'recovery_success', True),
               ('Bàn giao đúng / ca cần dừng', 'refusal_accuracy', True),
               ('An toàn thanh toán', 'safety_rate', True), ('Hoàn thành giả', 'false_completion_count', False),
               ('Bàn giao đủ trường', 'handoff_completeness', True),
               ('Gọi model TB', 'mean_model_calls', False), ('Gọi tool TB (gồm kiểm chứng/dọn dẹp)', 'mean_tool_calls', False),
               ('Lập lại kế hoạch TB', 'mean_replans', False), ('Độ trễ TB (ms)', 'mean_latency_ms', False),
               ('Độ trễ trung vị (ms)', 'median_latency_ms', False),
               ('Độ trễ p95 (ms)', 'p95_latency_ms', False),
               ('Token vào TB', 'mean_input_tokens', False), ('Token ra TB (gồm reasoning)', 'mean_output_tokens', False),
               ('Tổng token vào', 'known_input_tokens', False), ('Tổng token ra', 'known_output_tokens', False)]
    for title, key, percent in metrics:
        values = [display(artifact['summary'][p].get(key), percent) for p in ('react', 'plan', 'hybrid')]
        lines.append('| ' + ' | '.join([title, *values]) + ' |')
    lines += ['', '## So sánh theo kịch bản', '',
              '| Kịch bản | ReAct đúng/tổng | Plan đúng/tổng | Lai đúng/tổng |', '|---|---:|---:|---:|']
    for scenario in artifact['scenarios']:
        counts = []
        for pattern in ('react', 'plan', 'hybrid'):
            group = [row for row in artifact['rows'] if row['scenario'] == scenario['name'] and row['pattern'] == pattern]
            counts.append(f"{sum(row['outcome_correct'] for row in group)}/{len(group)}" if group else 'N/A')
        lines.append('| ' + ' | '.join([scenario['name'], *counts]) + ' |')
    lines += ['', 'N/A nghĩa là không có mẫu thuộc nhóm đó hoặc nhà cung cấp không trả usage. Không quy token thiếu thành chi phí bằng 0.', '',
              '## Từng lượt', '',
              '| Kịch bản | Seed | Mẫu | Kỳ vọng | Kết quả | Đúng | An toàn | Model | Tool | Token vào/ra | ms | Lý do |',
              '|---|---:|---|---|---|---|---|---:|---:|---|---:|---|']
    for row in artifact['rows']:
        values = [row['scenario'], row['seed'], row['pattern'], row['expected'], row['status'],
                  row['outcome_correct'], row['safe'], row['model_calls'], row['total_tool_calls'],
                  f"{display(row['input_tokens'])}/{display(row['output_tokens'])}", row['latency_ms'], row['reason']]
        lines.append('| ' + ' | '.join(str(value).replace('|', '\\|').replace('\n', ' ') for value in values) + ' |')
    lines += ['', '## Dấu vết thực thi', '',
              'Chuỗi dưới đây ghi tool, trạng thái và lý do; các lượt đều được kiểm chứng độc lập với lời model.', '']
    for run in artifact['runs']:
        result = run['result']
        chain = ' → '.join(f"{event['tool']}[{event['phase']}]:{event['result']['status']}" for event in result['trace'])
        lines.append(f"- `{run['scenario']}/{run['seed']}/{result['pattern']}`: {chain or 'Chưa gọi tool'}. Kế hoạch: {len(result['plans'])}.")
        if include_trace:
            lines += ['', '<details><summary>Nhật ký đầy đủ</summary>', '', '```json',
                      json.dumps(run, ensure_ascii=False, indent=2), '```', '', '</details>', '']
    lines += ['', '## Cách đọc và giới hạn', '',
              'DONE chỉ được chấm khi đọc lại booking, kiểm vé, quyền, tổng tiền, giá đã kiểm tra và sổ giao dịch. '
              'HANDOFF sau khi đã trả tiền nhưng chưa đọc lại được vé không tự động là thanh toán sai; cần đối soát. '
              'Plan-then-Execute cố định được phép thất bại an toàn khi giá/chỗ thay đổi; kết quả vẫn tính không đạt ở ca khả thi.', '',
              'Kỳ vọng nghiệp vụ giống nhau cho cả ba mẫu. Seed chỉ điều khiển inventory và lịch chạy, không bảo đảm LLM tất định. '
              'Số đo là thống kê mô tả của lần chạy, chưa chứng minh khác biệt có ý nghĩa thống kê. '
              'Chi phí tiền chưa tính vì chưa cấu hình đơn giá. Lỗi gọi/parse model không được tính là từ chối đúng.', '',
              'HANDOFF đúng còn cần bằng chứng nghiệp vụ độc lập: đã tìm mà không có chuyến hợp lệ, '
              'thiếu quyền, cần duyệt, lỗi dịch vụ, thanh toán bị từ chối hoặc chưa đối soát được vé đã trả. '
              'Dừng do tham số sai/mã bịa không được chấm như từ chối đúng. Token ra có thể gồm reasoning của provider.', '',
              '## Thông tin tái lập', '', '```json', json.dumps(meta, ensure_ascii=False, indent=2), '```', '']
    target.write_text('\n'.join(lines), encoding='utf-8')


def format_result(result):
    labels = {'react': 'ReAct', 'plan': 'Plan-then-Execute', 'hybrid': 'Lai'}
    lines = [f"Mẫu: {labels[result['pattern']]} | Kết quả: {result['status']}", f"Lý do: {result['reason']}"]
    if result['booking']:
        booking = result['booking']
        lines += [f"Mã đặt chỗ: {booking['code']} | Vé: {booking['ticket']}",
                  f"Chuyến: {booking['flight_id']} | Tổng tiền: {booking['total']:,} {booking['currency']}"]
    if result['handoff']:
        handoff = result['handoff']
        lines += [f"Tiến triển đã ghi nhận: {handoff['progress']} | Người tiếp nhận: {handoff['next_owner']}",
                  f"Số giao dịch đã ghi: {len(handoff['charges'])}", f"Cần xử lý: {handoff['question']}"]
    metrics = result['metrics']
    lines.append(f"Model: {metrics['model_calls']} lần | Tool: {metrics['total_tool_calls']} lần | {metrics['latency_ms']:.1f} ms")
    return '\n'.join(lines)
