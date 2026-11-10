"""浏览器层守护测试（Playwright spec）的断言保真检查。

比较候选 spec 与基线（缺省为 git HEAD 中的同一文件）的断言与数据核对：
- 相同：签名不变；新增：允许；
- 改动：须有账本条目（assertion_id、kind=rewrite、reason）；
- 缺失：违规，除非账本以 kind=withdraw 写明理由与依据（业务预期已变更的确认来源）。
静态检查不代替实际运行。
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from . import gitops

SCRIPT = Path(__file__).parent / 'skills' / 'playwright-acceptance' / 'scripts' / 'spec-ast.mjs'


def analyze(spec, root):
    from .browsers import runtime_info
    env = dict(os.environ)
    info = runtime_info()
    if info and not env.get('PW_NODE_ROOT'):
        env['PW_NODE_ROOT'] = str(Path(info['node_modules']).parent)
    result = subprocess.run(['node', str(SCRIPT), 'analyze', str(spec), str(root)], capture_output=True, text=True,
                            timeout=120, env=env)
    if result.returncode:
        raise ValueError('spec 静态分析失败：' + result.stderr.strip()[-400:])
    return json.loads(result.stdout)['items']


def baseline_from_git(repo, relative):
    """HEAD 中同一文件的内容写到临时目录（保持相对路径）；HEAD 没有该文件时返回 None。"""
    try:
        raw = gitops.git(repo, 'show', f'HEAD:{relative}')
    except subprocess.CalledProcessError:
        return None
    directory = Path(tempfile.mkdtemp(prefix='sop-board-baseline-'))
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, directory


def check(candidate, candidate_root, *, baseline=None, baseline_root=None, ledger=()):
    """返回 dict(same, new, review, violation, no_baseline)；violation 或未配账本的 review 视为不合格。"""
    current = {item['assertion_id']: item for item in analyze(candidate, candidate_root)}
    report = dict(same=[], new=[], review=[], violation=[], no_baseline=baseline is None)
    if baseline is None:
        report['new'] = sorted(current)
        return report
    before = {item['assertion_id']: item for item in analyze(baseline, baseline_root)}
    records = {}
    for record in ledger or ():
        if isinstance(record, dict) and isinstance(record.get('assertion_id'), str):
            records.setdefault(record['assertion_id'], []).append(record)
    for aid in sorted(before.keys() | current.keys()):
        old, new = before.get(aid), current.get(aid)
        if old and new and old['signature'] == new['signature']:
            report['same'].append(aid)
        elif not old:
            report['new'].append(aid)
        elif new:
            ok = any(r.get('kind') == 'rewrite' and isinstance(r.get('reason'), str) and r['reason'].strip()
                     for r in records.get(aid, []))
            report['review' if ok else 'violation'].append(dict(assertion_id=aid, reason='断言的对象、匹配器、参数或条件变化',
                                                                 ledger=bool(ok), old=old.get('text'), new=new.get('text')))
        else:
            ok = any(r.get('kind') == 'withdraw' and all(isinstance(r.get(k), str) and r[k].strip() for k in ('reason', 'ref'))
                     for r in records.get(aid, []))
            report['review' if ok else 'violation'].append(dict(assertion_id=aid, reason='断言或数据核对缺失',
                                                                 ledger=bool(ok), old=old.get('text'), new=None))
    return report


def qualified(report):
    if report['violation']:
        raise ValueError('保真检查不合格：' + '；'.join(f"{v['assertion_id']} {v['reason']}（没有对应账本）"
                                                    for v in report['violation'][:5]))


def check_file(repo, relative, *, ledger=()):
    """按 git HEAD 基线检查仓库中的 spec。"""
    candidate = Path(repo) / relative
    found = baseline_from_git(repo, relative)
    if found is None:
        return check(candidate, repo, ledger=ledger)
    baseline, directory = found
    return check(candidate, repo, baseline=baseline, baseline_root=directory, ledger=ledger)
