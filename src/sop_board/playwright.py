"""随 sop-board 发行的 Web 验收技能及其命令入口。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .scenario_file import load_scenarios
from .service import fingerprint


class InputError(ValueError):
    pass


def bundled_skill():
    return Path(__file__).parent / 'skills' / 'playwright-acceptance'


def driver_skill():
    directory = os.environ.get('SOP_BOARD_SKILLS_DIR')
    custom = Path(directory).expanduser() / 'playwright-acceptance' if directory else None
    return custom if custom and (custom / 'SKILL.md').is_file() else bundled_skill()


def write_scope(directory, project, batch, source, scenes, output=None):
    scope = dict(project=project, batch=batch,
        scenario_file_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        scenarios={scene['id']: fingerprint(scene) for scene in scenes})
    path = output if output is not None else directory / 'scope.json'
    path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def guide():
    return f"""Flow Board Web 场景验收

内置技能：{bundled_skill() / 'SKILL.md'}
当前执行驱动：{driver_skill() / 'SKILL.md'}
模板：{bundled_skill() / 'templates'}

启动看板：sop-board <工作区目录> --data <仓库外数据目录> --port 8787
Web 任务由看板提供 scenarios.yaml、pending.json、scope.json 和 entries.json。
读取内置技能，复用已打开的 Chrome；同一 worker 共用 CDP，每条用例单独录屏和 trace。
Playwright 依赖在验收工作目录安装：@playwright/test >=1.64 和 ffmpeg。

转换结果：sop-board playwright convert --report <报告.json> --out <任务目录> \\
  --scope <scope.json> --scenario-file <scenarios.yaml> --pending <pending.json> --executor <执行者>
自动任务只在任务目录写结果，看板负责登记；不要另建批次或自行提交第二个轮次。
手工试跑：sop-board playwright scope --workspace <工作区键> --scenario-file <原文> --out <scope.json>
手工登记：sop-board playwright submit --help（使用当前工作区 API，无需令牌）
配对标签：sop-board playwright tags --pairs <pairs.json> --out <cases.json>

完整执行默认拒绝漏测；部分试跑显式加 --allow-partial 并检查 coverage.json。
不依赖 team-workflow 或 tw 命令。详见内置技能 references/contracts.md、references/run-scope.md。
"""


def _script(name, arguments):
    argv = sys.argv
    try:
        script = bundled_skill() / 'scripts' / name
        sys.argv = [str(script), *arguments]
        try:
            runpy.run_path(str(script), run_name='__main__')
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else 1
        return 0
    finally:
        sys.argv = argv


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _submit(args):
    url = (args.url or os.environ.get('SOP_BOARD_URL', '')).rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputError('须提供不含凭证/查询参数的 http(s) SOP_BOARD_URL 或 --url')
    scope = json.loads(Path(args.scope).read_text(encoding='utf-8'))
    source = Path(args.scenario_file).read_bytes()
    if scope.get('scenario_file_sha256') != hashlib.sha256(source).hexdigest():
        raise InputError('scope 与场景原文版本不一致')
    document = load_scenarios(source.decode(), check_entry_references=False)
    if scope.get('batch') != document['metadata']['batch'] or not isinstance(scope.get('project'), str) or not scope['project'].strip():
        raise InputError('scope 的工作区或批次不合格')
    actual = {s['id']: fingerprint(s) for node in document['sops'] for s in node['scenarios']}
    expected = scope.get('scenarios')
    if not isinstance(expected, dict) or not expected or any(actual.get(sid) != value for sid, value in expected.items()):
        raise InputError('scope 中的场景内容指纹与原文不一致')
    product = json.loads(Path(args.product).read_text(encoding='utf-8'))
    product = dict(commit=product.get('commit'), digest=product.get('digest') or product.get('product_digest') or product.get('product_fingerprint'), dirty=product.get('dirty'))
    if not all(isinstance(product[k], str) and product[k].strip() for k in ('commit', 'digest')) or type(product['dirty']) is not bool:
        raise InputError('产品快照须包含 commit、digest/product_digest/product_fingerprint 和 dirty 布尔值')
    result_file = Path(args.results).resolve()
    rows = [json.loads(line) for line in result_file.read_text(encoding='utf-8').splitlines() if line.strip()]
    seen, uploads = set(), []
    for row in rows:
        sid = row.get('scenario') if isinstance(row, dict) else None
        if not isinstance(sid, str) or sid not in expected or sid in seen:
            raise InputError('结果含重复或范围外编号')
        seen.add(sid)
        if row.get('status') not in ('pass', 'fail', 'blocked', 'skip') or not all(isinstance(row.get(k), str) and row[k].strip() for k in ('actual', 'executed_at', 'executor')):
            raise InputError(f'{sid} 结果字段不完整')
        stamp = datetime.fromisoformat(row['executed_at'].replace('Z', '+00:00'))
        if stamp.utcoffset() is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise InputError(f'{sid} executed_at 须为带时区的实际执行时间')
        if row['status'] in ('blocked', 'skip') and not row.get('finding'):
            raise InputError(f'{sid} 缺少 finding')
        evidence = row.get('evidence')
        if not isinstance(evidence, list) or row['status'] in ('pass', 'fail') and not evidence:
            raise InputError(f'{sid} 缺少证据数组')
        data = []
        for ref in evidence:
            if not isinstance(ref, str) or Path(ref).is_absolute() or '..' in Path(ref).parts:
                raise InputError('证据须为结果目录内的相对路径')
            path = result_file.parent / ref
            if result_file.parent not in path.resolve().parents or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise InputError('证据不存在、越界或超过 64 MiB')
            data.append((path.name, path.read_bytes()))
        uploads.append(data)
    if not rows or not args.allow_partial and seen != set(expected):
        raise InputError('结果未覆盖 scope；部分试跑须显式 --allow-partial')
    base = f"/api/v1/workspaces/{quote(scope['project'], safe='')}/batches/{quote(scope['batch'], safe='')}"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def request(path, data=None, extra=None):
        headers = dict(extra or {})
        with opener.open(Request(url + path, data=data, headers=headers), timeout=30) as response:
            return response.read()
    # 上传任何证据之前先核对看板原文，不能把本轮登记到其他版本。
    if hashlib.sha256(request(base + '/scenario-file')).hexdigest() != scope['scenario_file_sha256']:
        raise InputError('看板当前场景原文已变化或批次不匹配，未登记结果')
    for row, artifacts in zip(rows, uploads):
        refs = []
        for name, data in artifacts:
            result = json.loads(request('/api/v1/evidence', data, {'Content-Type': 'application/octet-stream', 'X-Evidence-Name': quote(name, safe='')}))
            refs.append(dict(name=name, sha256=result['sha256']))
        row.update(evidence=refs, fingerprint=expected[row['scenario']])
    body = dict(idempotency_key=args.idempotency_key, scenario_file_sha256=scope['scenario_file_sha256'],
        product=product, submitted_by=args.submitted_by, results=rows)
    result = json.loads(request(base + '/rounds', json.dumps(body, ensure_ascii=False).encode(), {'Content-Type': 'application/json'}))
    print(json.dumps(result, ensure_ascii=False))


def command(arguments):
    if arguments and arguments[0] in ('convert', 'tags'):
        return _script('to_scenarios.py' if arguments[0] == 'convert' else 'case_tags.py', arguments[1:])
    parser = argparse.ArgumentParser(prog='sop-board playwright')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('guide')
    sub.add_parser('convert', add_help=False, help='转换 Playwright JSON 报告，参数用 convert --help 查看')
    sub.add_parser('tags', add_help=False, help='从配对文件生成标签，参数用 tags --help 查看')
    scope = sub.add_parser('scope', help='从当前场景原文创建范围快照')
    scope.add_argument('--workspace', '--project', dest='project', required=True, help='看板工作区键；--project 为旧参数别名')
    scope.add_argument('--scenario-file', required=True)
    scope.add_argument('--pending')
    scope.add_argument('--out', required=True)
    submit = sub.add_parser('submit', help='手工试跑登记到原工作区/批次，自动任务不要调用')
    for name in ('results', 'scope', 'scenario-file', 'product', 'idempotency-key', 'submitted-by'):
        submit.add_argument('--' + name, required=True)
    submit.add_argument('--url')
    submit.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args(arguments)
    try:
        if args.action == 'guide':
            print(guide())
        elif args.action == 'scope':
            if not args.project.strip():
                raise InputError('工作区键不能为空')
            source = Path(args.scenario_file)
            document = load_scenarios(source.read_text(encoding='utf-8'), check_entry_references=False)
            scenes = [s for n in document['sops'] for s in n['scenarios']]
            if args.pending:
                pending = json.loads(Path(args.pending).read_text(encoding='utf-8'))
                pending = pending.get('pending') if isinstance(pending, dict) else pending
                if not isinstance(pending, list):
                    raise InputError('pending 须为编号数组或含 pending 数组的对象')
                ids = [item.get('scenario') or item.get('id') if isinstance(item, dict) else item for item in pending]
                if not all(isinstance(sid, str) for sid in ids) or len(ids) != len(set(ids)) or set(ids) - {s['id'] for s in scenes}:
                    raise InputError('pending 含重复或未知场景编号')
                scenes = [s for s in scenes if s['id'] in ids]
            if not scenes:
                raise InputError('没有待执行场景')
            out = Path(args.out)
            if out.resolve() == source.resolve():
                raise InputError('scope 输出不能覆盖场景原文')
            out.parent.mkdir(parents=True, exist_ok=True)
            write_scope(out.parent, args.project, document['metadata']['batch'], source, scenes, output=out)
            print(out)
        else:
            _submit(args)
        return 0
    except Exception as error:
        # 不打印请求头或令牌；HTTP 失败不转发服务端正文。
        print(f'Playwright 操作失败：{type(error).__name__}' if not isinstance(error, InputError) else str(error), file=sys.stderr)
        return 1
