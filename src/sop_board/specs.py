"""浏览器层守护测试（Playwright spec）的断言保真检查。

比较候选 spec 与基线（缺省为 git HEAD 中的同一 spec 及其本地导入）的断言与数据核对：
- 相同：签名不变；新增：允许；
- 改动：须有账本条目（assertion_id、kind=rewrite、reason）；
- 缺失：违规，除非账本以 kind=withdraw 写明理由与依据（业务预期已变更的确认来源）。
静态检查不代替实际运行。
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import gitops

SCRIPT = Path(__file__).parent / 'skills' / 'playwright-acceptance' / 'scripts' / 'spec-ast.mjs'


def analyze(spec, root, *, head=None, promotion=False):
    """分析 spec 的断言。head 为仓库目录时，root 是该仓库 HEAD 的临时副本：分析器按需从 HEAD 取出 spec 的本地导入与任务资产。"""
    from .browsers import runtime_info
    env = {k: v for k, v in os.environ.items() if k != 'SOP_SPEC_BASELINE_REPO'}
    info = runtime_info()
    if info and not env.get('PW_NODE_ROOT'):
        env['PW_NODE_ROOT'] = str(Path(info['node_modules']).parent)
    if head is not None:
        env['SOP_SPEC_BASELINE_REPO'] = str(head)
    args = ['node', str(SCRIPT), 'analyze', str(spec), str(root)]
    if promotion:
        args += ['promotion', str(Path(spec).absolute().parent)]
    result = subprocess.run(args, capture_output=True, text=True,
                            timeout=120, env=env)
    if result.returncode:
        raise ValueError('spec 静态分析失败：' + result.stderr.strip()[-400:])
    return json.loads(result.stdout)['items']


def in_head(repo, relative):
    """git HEAD 里有没有这个文件。"""
    try:
        gitops.git(repo, 'cat-file', '-e', f'HEAD:{relative}')
        return True
    except subprocess.CalledProcessError:
        return False


@contextmanager
def head_baseline(repo, relative):
    """基线：HEAD 中的同一 spec 写到临时目录（保持相对路径），给出 (spec 路径, 临时目录)；HEAD 没有该文件时给出 None。

    spec 按规定只经 ./fixtures.mjs、./library.mjs 导入，分析时这些本地导入（及任务资产）由分析器按需从 HEAD 取出，
    所以基线比较的是 HEAD 时的整套 spec，而不是 HEAD 的 spec 配上工作树里的夹具。临时目录用完即删。"""
    if not in_head(repo, relative):
        yield None
        return
    directory = Path(tempfile.mkdtemp(prefix='sop-board-baseline-'))
    try:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gitops.git(repo, 'show', f'HEAD:{relative}'))
        yield path, directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def check(candidate, candidate_root, *, baseline=None, baseline_root=None, baseline_head=None, ledger=(), promotion=False):
    """返回 dict(same, new, review, violation, no_baseline)；violation 或未配账本的 review 视为不合格。
    baseline_head：基线取自该仓库的 git HEAD（见 head_baseline）。
    promotion：首次沉淀，逻辑路径相对各自 spec 目录；直接 fixtures 导入是可替换的运行适配接口。"""
    try:
        current = {item['assertion_id']: item for item in analyze(candidate, candidate_root, promotion=promotion)}
    except ValueError as error:
        raise ValueError(f'候选 spec {candidate}：{error}') from error
    report = dict(same=[], new=[], review=[], violation=[], no_baseline=baseline is None, baseline=str(baseline) if baseline else None)
    if baseline is None:
        report['new'] = sorted(current)
        return report
    try:
        before = {item['assertion_id']: item for item in analyze(baseline, baseline_root, head=baseline_head, promotion=promotion)}
    except ValueError as error:
        raise ValueError(f'基线 spec {baseline}：{error}；要按规则写 spec 重新验收后再沉淀') from error
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
        raise ValueError(f"保真检查不合格（基线 {report.get('baseline')}）：" + '；'.join(f"{v['assertion_id']} {v['reason']}（没有对应账本）"
                                                    for v in report['violation'][:5]))


def check_file(repo, relative, *, ledger=()):
    """按 git HEAD 基线检查仓库中的 spec。"""
    candidate = Path(repo) / relative
    with head_baseline(repo, relative) as found:
        if found is None:
            return check(candidate, repo, ledger=ledger)
        baseline, directory = found
        return check(candidate, repo, baseline=baseline, baseline_root=directory, baseline_head=repo, ledger=ledger)
