"""独立只读巡检；不写场景轮次，不参与影响判断。"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from .entries import web_evidence
from .service import now
from .spec_library import read, write, sha, ReadStore


def locator(item):
    if item.get('testid'):
        return dict(testid=item['testid'])
    names = item.get('name') or item.get('label') or item.get('title') or []
    if item.get('role') and names:
        return dict(role=item['role'], name=names[0]['text'], exact=True)
    return dict(expression=item['locator']) if item.get('locator') else None


def candidate(entry, page, source):
    targets = []
    for item in page.get('controls', []):
        targets.append(dict(ref=page['id'] + '/' + item['id'], locator=locator(item), count=1))
    for item in page.get('tables', []):
        targets.append(dict(ref=page['id'] + '/' + item['id'], locator=locator(item), count=1))
    route = page.get('route')
    return dict(id=page['id'], entry=entry, identity=None, source=source,
        page=page['id'], controls=[t['ref'] for t in targets], pages=[route] if route else [],
        reach=[dict(action='goto', path=route)] if route else [dict(action='unresolved', note=page.get('reach', []))],
        targets=targets, allow_requests=[dict(method=r['method'], path=r['path'], purpose='待审核：地图没有副作用语义')
            for control in page.get('controls', []) for r in control.get('requests', [])],
        forbidden_actions=['登录', '发送', '保存', '删除', '标为已读', '未声明的动作'],
        review=dict(by=None, at=None, plan_sha256=None),
        uncovered=['仅检查定位与数量，不验证场景预期', '请求业务用途与到达步骤须由项目审核者确认'])


def plan(workspace, store, entry, atlas=None, surface=None):
    if atlas:
        document = read(atlas)
        if 'entries' in document:
            document = next(e for e in document['entries'] if e['entry'] == entry)
        source = dict(kind='atlas', file=str(Path(atlas).resolve()), sha256=sha(Path(atlas).read_bytes()))
        rows = [candidate(entry, p, source) for p in document['pages']]
    elif surface:
        text = Path(surface).read_text(encoding='utf-8')
        source = dict(kind='surface-map', file=str(Path(surface).resolve()), sha256=sha(text.encode()))
        structured = re.search(chr(96)*3 + r'(?:patrol-map|json)\s*\n(.*?)\n' + chr(96)*3, text, re.S)
        if structured:
            document = json.loads(structured[1])
            rows = [candidate(entry, page, source) for page in document['pages']]
        else:
            rows = []
            for line in text.splitlines():
                if not line.startswith('|') or re.match(r'^\|[- :|]+\|$', line):
                    continue
                cells = [c.strip().strip(chr(96)) for c in line.strip('|').split('|')]
                if not cells or cells[0].startswith('<') or cells[0] in ('入口名（verify）', '目标视图', '原场景编号/步骤', '遮挡或异常'):
                    continue
                route = next((r for c in cells for r in re.findall(r'(?<!\w)(/[A-Za-z0-9_./:-]+)', c)), None)
                item = candidate(entry, dict(id='map-' + str(len(rows) + 1), route=route, reach=[line], controls=[], tables=[]), source)
                item['source_text'] = line
                item['targets'] = [dict(ref=cells[0], locator=None, count=1)]
                item['uncovered'].append('文字地图的定位与步骤尚未结构化，请按原文补齐后审核')
                rows.append(item)
            if not rows:
                raise ValueError('surface-map 未包含可生成候选的页面记录')
    else:
        from .atlas import directory
        with store.connection() as db:
            latest = db.execute('SELECT version FROM atlas_versions WHERE workspace=? AND entry=? ORDER BY version DESC LIMIT 1',
                                (workspace.name, entry)).fetchone()
        if not latest:
            raise ValueError('入口没有已登记 atlas；请提供 --surface-map 或 --atlas')
        return plan(workspace, store, entry, atlas=directory(workspace, entry) / ('v' + str(latest[0])) / 'atlas.json')
    return dict(version=1, generated_at=now(), entries=rows, note='候选未审核，不访问浏览器；补齐请求用途、数量和到达步骤后由实际审核者签署')


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board playwright patrol')
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('plan', 'run'):
        sub = actions.add_parser(action)
        sub.add_argument('--data', type=Path)
        sub.add_argument('--entry', required=True)
        sub.add_argument('--out', type=Path, required=True)
        if action == 'plan':
            source = sub.add_mutually_exclusive_group()
            source.add_argument('--atlas', type=Path)
            source.add_argument('--surface-map', type=Path)
        else:
            sub.add_argument('--plan', type=Path, help='缺省读取该入口规范库 patrol.json')
            sub.add_argument('--cdp', help='已有测试浏览器的 CDP 地址；否则读条目身份或 PW_CDP_ENDPOINT')
            sub.add_argument('--tab', help='已有登录标签页的唯一 URL 子串；否则使用 PW_TAB 或入口地址')
    args = parser.parse_args(arguments)
    try:
        from .workspace import Workspace
        workspace = Workspace(data=args.data, readonly=True)
        if not workspace.configured:
            raise ValueError('工作区配置不合格')
        declaration = workspace.entries.get(args.entry, {})
        if declaration.get('surface') != 'web':
            raise ValueError('--entry 须为已声明 Web 入口')
        if args.action == 'plan':
            if args.out.exists():
                raise ValueError('计划输出已存在，请另选文件，保留原计划')
            result = plan(workspace, ReadStore(workspace.data), args.entry, args.atlas, args.surface_map)
            write(args.out, result)
            print(args.out)
            return 0
        root = workspace.spec_library(args.entry)
        file = args.plan or (root / 'patrol.json' if root else None)
        if file is None:
            raise ValueError('请提供 --plan 或声明规范库 patrol.json')
        value = read(file)
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('entries'), list):
            raise ValueError('patrol.json 须为 version=1、entries 数组')
        if args.out.exists():
            raise ValueError('巡检输出目录已存在，请另选目录，保留首次证据')
        if any(args.out.resolve().is_relative_to(p) for p in workspace.repos.values()):
            raise ValueError('巡检过程材料须放在产品仓库外')
        address = os.environ.get(declaration.get('address_env', ''), '')
        if urlsplit(address).scheme not in ('http', 'https'):
            raise ValueError('入口当前地址环境变量缺失或不合格')
        args.out.mkdir(parents=True)
        payload = dict(plan=value, entry=args.entry, node_root=str(workspace.data), address=address, identities=declaration.get('identities', []),
            cdp=args.cdp, tab=args.tab or os.environ.get('PW_TAB') or address, web_evidence=web_evidence(declaration),
            plan_sha256=sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()),
            started_at=now(), plan_file=str(file.resolve()))
        write(args.out / 'patrol-input.json', payload)
        script = Path(__file__).parent / 'skills/playwright-acceptance/scripts/patrol.mjs'
        result = subprocess.run(['node', str(script), str(args.out / 'patrol-input.json')], check=False)
        return result.returncode
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('巡检操作失败：' + str(error), file=sys.stderr)
        return 1
