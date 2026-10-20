#!/usr/bin/env python3
"""从 sop-board 的配对文件生成“场景 → 上游用例”映射，供 spec 作为 Playwright tag 使用。

配对文件格式见 本技能 references/contracts.md 的配对文件约定：
  {"covered": [{"case", "scenarios", "note"}], "conflict": [...], "disposed": [{"case", "state", "reason", "confirmed_by"}]}

输出 cases.json：
  {"scenarios": {"SOP-02-P01": ["CASE-037"]}, "cases": {"CASE-037": ["SOP-02-P01"]},
   "disposed": {"CASE-040": {"state": "deferred", "reason": "...", "confirmed_by": "..."}}}

tag 只能来自已确认的配对。存在 conflict 时默认退出码 2，除非 --allow-conflict（此时 conflict 中的用例不生成 tag）。
只用 Python 3.9+ 标准库。
"""
import argparse
import json
import re
import sys
from pathlib import Path

TOKEN = re.compile(r'^\S+$')


class PairsError(Exception):
    pass


def _rows(pairs, key):
    rows = pairs.get(key, [])
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise PairsError('%s 应为数组' % key)
    return rows


def _ident(value, where):
    if not isinstance(value, str) or not TOKEN.match(value):
        raise PairsError('%s：编号应为不含空白的字符串，得到 %r' % (where, value))
    return value


def build(pairs):
    """返回 (mapping, conflicts)。mapping 含 scenarios、cases、disposed。"""
    if not isinstance(pairs, dict):
        raise PairsError('配对文件顶层应为对象')
    unknown = set(pairs) - {'covered', 'conflict', 'disposed'}
    if unknown:
        raise PairsError('配对文件含未知字段：' + ', '.join(sorted(unknown)))
    scenarios = {}
    cases = {}
    for index, row in enumerate(_rows(pairs, 'covered')):
        where = 'covered[%d]' % index
        if not isinstance(row, dict):
            raise PairsError(where + ' 应为对象')
        case = _ident(row.get('case'), where + '.case')
        linked = row.get('scenarios')
        if not isinstance(linked, list) or not linked:
            raise PairsError(where + '.scenarios 应为非空数组')
        for ordinal, scenario in enumerate(linked):
            scenario = _ident(scenario, '%s.scenarios[%d]' % (where, ordinal))
            scenarios.setdefault(scenario, set()).add(case)
            cases.setdefault(case, set()).add(scenario)
    disposed = {}
    for index, row in enumerate(_rows(pairs, 'disposed')):
        where = 'disposed[%d]' % index
        if not isinstance(row, dict):
            raise PairsError(where + ' 应为对象')
        case = _ident(row.get('case'), where + '.case')
        disposed[case] = {key: row.get(key) for key in ('state', 'reason', 'confirmed_by')}
    conflicts = []
    for index, row in enumerate(_rows(pairs, 'conflict')):
        where = 'conflict[%d]' % index
        if not isinstance(row, dict):
            raise PairsError(where + ' 应为对象')
        conflicts.append(_ident(row.get('case'), where + '.case'))
    mapping = {
        'scenarios': {key: sorted(value) for key, value in sorted(scenarios.items())},
        'cases': {key: sorted(value) for key, value in sorted(cases.items())},
        'disposed': dict(sorted(disposed.items())),
    }
    return mapping, conflicts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pairs', required=True, help='sop-board 配对文件（JSON）')
    parser.add_argument('--out', required=True, help='输出 cases.json 的路径（放在仓库外）')
    parser.add_argument('--allow-conflict', action='store_true', help='存在 conflict 时仍输出，冲突用例不生成 tag')
    args = parser.parse_args(argv)
    try:
        pairs = json.loads(Path(args.pairs).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('读取配对文件失败：%s' % exc, file=sys.stderr)
        return 2
    try:
        mapping, conflicts = build(pairs)
    except PairsError as exc:
        print('配对文件格式错误：%s' % exc, file=sys.stderr)
        return 2
    if conflicts:
        print('配对文件有 %d 条 conflict 用例：%s' % (len(conflicts), ', '.join(conflicts)), file=sys.stderr)
        if not args.allow_conflict:
            print('先与测试同事确认预期并改为 covered 或 disposed，或加 --allow-conflict 先行输出。', file=sys.stderr)
            return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('已写入 %s：%d 个场景，%d 条已覆盖用例，%d 条已处置用例。' % (
        out, len(mapping['scenarios']), len(mapping['cases']), len(mapping['disposed'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
