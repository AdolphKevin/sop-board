#!/usr/bin/env python3
"""把 Playwright JSON report 转成 sop-board 的场景结果契约（results.jsonl），并把证据复制到结果目录内。

用法：
  to_scenarios.py --report <PW_OUT>/report.json --out <目录> --executor "<执行者>"
                  [--finding SOP-02-P01=Q3 ...] [--pending pending.json] [--scope scope.json --scenario-file scenarios.yaml]
                  [--allow-partial] [--session <会话>] [--skip-large]

约定：
- 场景编号 = 测试标题的第一个空白分隔词，例如 "SOP-02-P01 人工接管…" → SOP-02-P01。无编号的测试跳过并提示。
- passed → pass；skipped → skip；failed / timedOut / interrupted → fail，
  但错误信息以“受阻：”开头时 → blocked。每个测试取最后一次结果（不建议 retries）。
- 同一场景有多条测试时合并，状态取最差：fail > blocked > skip > pass；附件按测试隔离，避免覆盖。
- pending/scope 默认要求结果完整；部分试跑须 --allow-partial。coverage.json 单独记录范围，不更改结果契约。
- scope 对比报告注解与原始场景文件摘要，防止批次、编号或指纹串用。
- blocked 与 skip 必须用 --finding 指定问题编号（sop-board 契约要求）；pass 与 fail 至少要有一份证据。
- 证据复制到 <out>/evidence/<场景编号>/，结果文件里用相对路径引用；单个文件超过 64 MiB 会被 sop-board 拒绝，
  默认报错，--skip-large 则跳过该文件并提示。
- executed_at = 该测试的 startTime + duration，带 +00:00 时区，秒精度。
只用 Python 3.9+ 标准库。
"""
import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BLOCKED_PREFIX = '受阻：'
EVIDENCE_LIMIT_BYTES = 64 * 1024 * 1024
RANK = {'fail': 3, 'blocked': 2, 'skip': 1, 'pass': 0}
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
ACTUAL_LIMIT = 300
UNSAFE = re.compile(r'[^A-Za-z0-9._\u4e00-\u9fff-]+')


class ConvertError(Exception):
    pass


def strip_ansi(text):
    return ANSI.sub('', text or '')


def error_message(result):
    error = result.get('error') or {}
    message = error.get('message')
    if not message:
        for item in result.get('errors') or []:
            if item.get('message'):
                message = item['message']
                break
    message = strip_ansi(message or '').strip()
    if message.startswith('Error: '):
        message = message[len('Error: '):]
    return message


def first_line(text):
    for line in (text or '').splitlines():
        if line.strip():
            return line.strip()
    return ''


def truncate(text, limit=ACTUAL_LIMIT):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def scenario_id(title):
    parts = title.split()
    if not parts:
        return None
    head = parts[0]
    # 场景编号来自 SOP（沿用原连字符规则，如 SOP-12-P01）或用例合并（与用例编号规则相同，如 TC_ORD_005）。
    if re.match(r'^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9-]+$', head) or re.fullmatch(r'TC_[A-Z0-9_]{3,}_[0-9]{3}', head):
        return head
    return None


def walk_specs(suite):
    for spec in suite.get('specs') or []:
        yield spec
    for child in suite.get('suites') or []:
        for spec in walk_specs(child):
            yield spec


def last_result(test):
    results = test.get('results') or []
    return results[-1] if results else None


def classify(result):
    status = result.get('status')
    if status == 'passed':
        return 'pass'
    if status == 'skipped':
        return 'skip'
    if status in ('failed', 'timedOut', 'interrupted'):
        return 'blocked' if error_message(result).startswith(BLOCKED_PREFIX) else 'fail'
    raise ConvertError('未知的测试状态：%r' % status)


def step_summary(result):
    done = []
    failed = None
    for step in result.get('steps') or []:
        title = step.get('title') or ''
        if step.get('error'):
            failed = title
            break
        done.append(title)
    return done, failed


def describe(result, status, test):
    """用步骤与错误信息拼出 actual（实际结果）。"""
    done, failed_step = step_summary(result)
    message = error_message(result)
    if status == 'pass':
        text = '已完成 %d 步：%s' % (len(done), '；'.join(done)) if done else '通过'
    elif status == 'skip':
        reasons = [a.get('description') for a in (test.get('annotations') or []) if a.get('type') == 'skip' and a.get('description')]
        text = '跳过：' + (reasons[0] if reasons else '未说明原因')
    elif status == 'blocked':
        text = first_line(message)
    else:
        where = ('第 %d 步「%s」' % (len(done) + 1, failed_step)) if failed_step else ('第 %d 步后' % len(done) if done else '')
        detail = first_line(message) or ('超时' if result.get('status') == 'timedOut' else '失败')
        text = (where + '失败：' + detail) if where else detail
    return truncate(text)


def executed_at(result):
    start = result.get('startTime')
    if not start:
        raise ConvertError('结果缺少 startTime')
    moment = datetime.fromisoformat(start.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment + timedelta(milliseconds=result.get('duration') or 0)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def safe_name(name, fallback):
    cleaned = UNSAFE.sub('-', name or '').strip('-.')
    return cleaned or fallback


def copy_evidence(result, dest, report_dir, skip_large, warnings, namespace=None):
    """把附件复制到 dest，返回相对 dest 父目录(out) 的 posix 路径列表。"""
    refs = []
    for index, item in enumerate(result.get('attachments') or [], 1):
        name = item.get('name') or 'attachment'
        source = None
        body = None
        if item.get('path'):
            source = Path(item['path'])
            if not source.is_absolute():
                source = report_dir / source
            if not source.is_file():
                warnings.append('附件不存在，已跳过：%s' % source)
                continue
            suffix = source.suffix
            size = source.stat().st_size
        elif item.get('body'):
            body = base64.b64decode(item['body'])
            suffix = guess_suffix(item.get('contentType'))
            size = len(body)
        else:
            continue
        if size > EVIDENCE_LIMIT_BYTES:
            note = '%s 超过 64 MiB（%.1f MiB）' % (source or name, size / 1024 / 1024)
            if not skip_large:
                raise ConvertError(note + '；sop-board 会拒绝，请截取片段或加 --skip-large 跳过')
            warnings.append(note + '，已跳过')
            continue
        stem = safe_name(Path(name).stem if source else name, 'attachment-%d' % index)
        prefix = (namespace + '-') if namespace else ''
        target = dest / ('%s%02d-%s%s' % (prefix, index, stem, suffix))
        dest.mkdir(parents=True, exist_ok=True)
        if source is not None:
            shutil.copyfile(source, target)
        else:
            target.write_bytes(body)
        refs.append(target.relative_to(dest.parent.parent).as_posix())
    return refs


def guess_suffix(content_type):
    table = {
        'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'video/webm': '.webm',
        'text/plain': '.txt', 'text/markdown': '.md', 'application/json': '.json', 'application/zip': '.zip',
    }
    return table.get((content_type or '').split(';')[0].strip(), '')


def collect(report):
    """按场景汇总：返回 {sid: [(spec_title, test, result)]} 与无编号标题列表。"""
    grouped = {}
    unnamed = []
    for suite in report.get('suites') or []:
        for spec in walk_specs(suite):
            sid = scenario_id(spec.get('title') or '')
            if not sid:
                unnamed.append(spec.get('title') or '(无标题)')
                continue
            for test in spec.get('tests') or []:
                result = last_result(test)
                if result is None:
                    continue
                grouped.setdefault(sid, []).append((spec.get('title'), test, result))
    return grouped, unnamed


def convert(report, out, executor, findings, session=None, pending_ids=None, skip_large=False, report_dir=None,
            allow_partial=False, scope=None):
    out = Path(out)
    report_dir = Path(report_dir) if report_dir else Path.cwd()
    grouped, unnamed = collect(report)
    warnings = ['测试标题没有场景编号，未纳入结果：%s' % title for title in unnamed]
    if not grouped:
        global_errors = [strip_ansi(item.get('message', '')).split('\n    at ')[0].strip() for item in report.get('errors') or []]
        if global_errors:
            raise ConvertError('本轮没有执行任何场景，Playwright 在开跑前报错：\n' + '\n'.join(global_errors))
        raise ConvertError('报告里没有带场景编号的测试')
    report_scope = report.get('config', {}).get('metadata', {}).get('sopScope')
    annotated = any(a.get('type') == 'sop-scope' for entries in grouped.values() for _, test, _ in entries for a in test.get('annotations', []))
    if scope is None and (report_scope or annotated):
        raise ConvertError('报告已绑定 scope，转换时须提供同一 --scope 和 --scenario-file')
    expected = set(pending_ids) if pending_ids is not None else (set(scope['scenarios']) if scope else None)
    missing = sorted(expected - set(grouped)) if expected is not None else []
    extra = sorted(set(grouped) - expected) if expected is not None else []
    if extra:
        raise ConvertError('报告包含执行范围外的场景：' + ', '.join(extra))
    if missing and not allow_partial:
        raise ConvertError('执行范围缺少结果：' + ', '.join(missing) + '；完整执行不能遗漏，部分试跑须显式加 --allow-partial')
    if scope:
        for sid, entries in grouped.items():
            if sid not in scope['scenarios']:
                raise ConvertError('场景不在 scope 中：' + sid)
            binding = {key: scope[key] for key in ('project', 'batch', 'scenario_file_sha256')}
            binding.update(scenario=sid, fingerprint=scope['scenarios'][sid])
            for _, test, result in entries:
                annotations = [a.get('description') for a in test.get('annotations', []) if a.get('type') == 'sop-scope']
                # 声明式 skip 不运行 fixture，靠配置内的同一范围快照绑定。
                if not annotations and result.get('status') == 'skipped' and report_scope == scope:
                    continue
                if len(annotations) != 1 or annotations[0] != json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':')):
                    raise ConvertError('报告的批次或场景指纹与 scope 不一致：' + sid)
    rows = []
    problems = []
    for sid in sorted(grouped):
        entries = grouped[sid]
        statuses = [(classify(result), title, test, result) for title, test, result in entries]
        worst = max(statuses, key=lambda item: RANK[item[0]])
        status = worst[0]
        evidence = []
        actuals = []
        latest = None
        for test_index, (item_status, title, test, result) in enumerate(statuses, 1):
            namespace = 'test-%03d' % test_index if len(statuses) > 1 else None
            evidence.extend(copy_evidence(result, out / 'evidence' / sid, report_dir, skip_large, warnings, namespace))
            if item_status == status:
                actuals.append(describe(result, item_status, test) if len(statuses) == 1 else '%s：%s' % (title, describe(result, item_status, test)))
            moment = executed_at(result)
            latest = moment if latest is None or moment > latest else latest
        row = {
            'scenario': sid,
            'status': status,
            'actual': truncate('；'.join(actuals)),
            'executed_at': latest,
            'executor': executor,
            'evidence': evidence,
        }
        if session:
            row['session'] = session
        if sid in findings:
            row['finding'] = findings[sid]
        elif status in ('blocked', 'skip'):
            problems.append('%s 为 %s，请用 --finding %s=<问题编号> 指定问题' % (sid, status, sid))
        if status in ('pass', 'fail') and not evidence:
            problems.append('%s 为 %s 但没有任何证据附件，请在 spec 里截图或开启录屏' % (sid, status))
        if pending_ids is not None and sid not in pending_ids:
            problems.append('%s 不在 pending 清单里，核对场景编号或任务身份' % sid)
        rows.append(row)
    if missing:
        warnings.append('部分试跑，执行范围中尚无结果的场景：%s' % ', '.join(missing))
    if problems:
        raise ConvertError('\n'.join(problems))
    return rows, warnings


def parse_findings(values):
    findings = {}
    for value in values or []:
        if '=' not in value:
            raise ConvertError('--finding 格式为 场景编号=问题编号：%s' % value)
        sid, finding = value.split('=', 1)
        if not sid.strip() or not finding.strip():
            raise ConvertError('--finding 格式为 场景编号=问题编号：%s' % value)
        findings[sid.strip()] = finding.strip()
    return findings


def load_pending(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    items = data.get('pending') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ConvertError('pending 文件应为 看板 pending.json（数组或含 pending 数组的对象）')
    ids = []
    for item in items:
        if isinstance(item, dict) and (item.get('id') or item.get('scenario')):
            ids.append(item.get('id') or item['scenario'])
        elif isinstance(item, str) and item.strip():
            ids.append(item)
        else:
            raise ConvertError('pending 含无有效场景编号的条目')
    if not all(isinstance(sid, str) and sid.strip() for sid in ids) or len(ids) != len(set(ids)):
        raise ConvertError('pending 的场景编号应为不重复的非空字符串')
    return ids


def load_scope(path, scenario_file):
    if not scenario_file:
        raise ConvertError('--scope 须同时指定 --scenario-file，以核对原始场景文件版本')
    scope = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(scope, dict) or not all(isinstance(scope.get(k), str) and scope[k].strip() for k in ('project', 'batch')):
        raise ConvertError('scope 须包含非空 project、batch')
    sha = scope.get('scenario_file_sha256')
    if sha != hashlib.sha256(Path(scenario_file).read_bytes()).hexdigest():
        raise ConvertError('scope 的 scenario_file_sha256 与场景文件不一致')
    scenarios = scope.get('scenarios')
    if not isinstance(scenarios, dict) or not scenarios or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and re.fullmatch(r'[a-f0-9]{64}', v)
            for k, v in scenarios.items()):
        raise ConvertError('scope.scenarios 须为场景编号到 SHA-256 内容指纹的非空对象')
    return scope


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--report', required=True, help='Playwright JSON reporter 输出')
    parser.add_argument('--out', required=True, help='结果目录（仓库外），写入 results.jsonl 与 evidence/')
    parser.add_argument('--executor', required=True, help='执行者，写入每条结果')
    parser.add_argument('--finding', action='append', default=[], help='场景编号=问题编号，可重复；blocked 与 skip 必填')
    parser.add_argument('--pending', help='看板 pending.json（数组或含 pending 数组的对象），用于核对编号')
    parser.add_argument('--session', help='可选的会话标识，写入 session 字段')
    parser.add_argument('--allow-partial', action='store_true', help='显式允许范围内部分场景尚无结果，并输出 coverage.json')
    parser.add_argument('--scope', help='上游批次及场景指纹快照；与运行时 PW_SCOPE 使用同一文件')
    parser.add_argument('--scenario-file', help='scope 所绑定的原始场景文件')
    parser.add_argument('--skip-large', action='store_true', help='跳过超过 64 MiB 的证据文件而不是报错')
    args = parser.parse_args(argv)
    try:
        report_path = Path(args.report)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        findings = parse_findings(args.finding)
        pending_ids = load_pending(args.pending) if args.pending else None
        scope = load_scope(args.scope, args.scenario_file) if args.scope else None
        if args.allow_partial and pending_ids is None and scope is None:
            raise ConvertError('--allow-partial 须提供 --pending 或 --scope，以列明未执行范围')
        rows, warnings = convert(report, args.out, args.executor, findings, session=args.session,
                                 pending_ids=pending_ids, skip_large=args.skip_large, report_dir=report_path.parent,
                                 allow_partial=args.allow_partial, scope=scope)
    except (OSError, ValueError) as exc:
        print('读取输入失败：%s' % exc, file=sys.stderr)
        return 2
    except ConvertError as exc:
        print('转换失败：\n%s' % exc, file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / 'results.jsonl'
    target.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    executed = sorted(row['scenario'] for row in rows)
    expected = sorted(pending_ids if pending_ids is not None else (scope['scenarios'] if scope else executed))
    mode = 'unscoped' if pending_ids is None and scope is None else ('partial' if args.allow_partial else 'complete')
    coverage = dict(mode=mode, scope_verified=scope is not None,
                    expected=expected, executed=executed, missing=sorted(set(expected) - set(executed)))
    if scope:
        coverage.update({key: scope[key] for key in ('project', 'batch', 'scenario_file_sha256')})
    (out / 'coverage.json').write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if pending_ids is None and scope is None:
        warnings.append('未提供 pending/scope，仅转换报告内结果，不能证明批次覆盖完整')
    for warning in warnings:
        print('提示：' + warning, file=sys.stderr)
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    summary = '，'.join('%s %d' % (key, counts[key]) for key in ('pass', 'fail', 'blocked', 'skip') if key in counts)
    print('已写入 %s：%d 个场景（%s）。' % (target, len(rows), summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
