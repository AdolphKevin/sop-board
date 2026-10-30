"""问题台账与决定记录（D98、D99）：看板原生记录问题、影响的场景、修复关联与人的决定，替代项目手写的 markdown 台账。

问题来源：执行结果行的 issue 字段（归并到已有问题）、界面对照中“按 SOP”的决定、人工新建。
人可把问题设为待处理、暂缓或已关闭；关联修复后按最近一次修复的状态显示修复中、待验证或已关闭。
决定记录只登记人给出的原话和姓名，命令不替人推断。
"""
import argparse
import json
from pathlib import Path
import sqlite3

from .errors import APIError
from .kernel import FIX_TERMINAL, S, sql_states
from .service import now

KINDS = ('product', 'scenario', 'environment', 'tool')
KIND_LABELS = dict(product='产品', scenario='场景写法', environment='环境', tool='工具')
MANUAL = ('open', 'deferred', 'closed')
LABELS = dict(open='待处理', fixing='修复中', verifying='待验证', closed='已关闭', deferred='暂缓')
VERIFYING = frozenset((S.WAIT_VALIDATION, S.WAIT_COMMIT, S.VERIFYING, S.PROMOTING, S.DEVELOPER_MERGE))
FIXED = frozenset((S.HANDLED, S.MERGED))
REOPENED = frozenset((S.ABANDONED, S.DEVELOPER, S.FAILED, S.VOID))
SOURCES = dict(session='会话登记', issue='问题台账', atlas='界面对照', merge='合并答复')


def status(stored, fix_status):
    """人设的暂缓、已关闭优先；否则按最近关联修复的状态显示。"""
    if stored in ('deferred', 'closed') or fix_status is None:
        return stored
    if fix_status in FIXED:
        return 'closed'
    if fix_status in VERIFYING:
        return 'verifying'
    return 'open' if fix_status in REOPENED else 'fixing'


def parse_scenarios(values):
    pairs = set()
    for item in values or []:
        batch, separator, scene = str(item).partition('#')
        if not separator or not batch or not scene:
            raise ValueError('场景须写成 批次#场景：' + str(item))
        pairs.add((batch, scene))
    return sorted(pairs)


def check_scenarios(db, workspace, pairs):
    missing = [b + '#' + s for b, s in pairs if not db.execute(
        'SELECT 1 FROM scenarios c JOIN batches b ON b.id=c.batch WHERE b.workspace=? AND b.name=? AND c.scenario=? AND c.current=1',
        (workspace, b, s)).fetchone()]
    if missing:
        raise ValueError('场景不在当前版本：' + '、'.join(missing))


def person(value, label='请填写决定人姓名（命令行用 --by）'):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label)
    return value.strip()


def find(db, workspace, number):
    row = db.execute('SELECT * FROM issues WHERE workspace=? AND number=?', (workspace, number)).fetchone()
    if row is None:
        raise ValueError(f'问题 {number} 不存在')
    return row


def record_decision(db, workspace, *, text, author, source, batch=None, issue=None):
    db.execute('INSERT INTO decisions(workspace,batch,issue,text,author,at,source) VALUES (?,?,?,?,?,?,?)',
               (workspace, batch, issue, text.strip(), author, now(), source))


def add(db, workspace, *, title, kind, by, scenarios=(), root_cause='', note='', source='issue'):
    if not isinstance(title, str) or not title.strip():
        raise ValueError('请填写问题标题')
    if kind not in KINDS:
        raise ValueError('类型应为 ' + '、'.join(KINDS))
    pairs = sorted(set(scenarios))
    check_scenarios(db, workspace, pairs)
    number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM issues WHERE workspace=?', (workspace,)).fetchone()[0]
    stamp = now()
    issue = db.execute("INSERT INTO issues(workspace,number,title,kind,root_cause,status,jira,created_at,created_by,updated_at) "
                       "VALUES (?,?,?,?,?,'open','',?,?,?)", (workspace, number, title.strip(), kind, root_cause or '', stamp, by, stamp)).lastrowid
    for batch, scene in pairs:
        link(db, issue, batch, scene)
    record_decision(db, workspace, text=note or f'新建问题：{title.strip()}', author=by, source=source, issue=issue)
    return number


def link(db, issue, batch, scene, result_id=None):
    db.execute('INSERT INTO issue_scenarios(issue,batch,scenario,result_id,linked_at) VALUES (?,?,?,?,?) '
               'ON CONFLICT(issue,batch,scenario) DO UPDATE SET result_id=COALESCE(excluded.result_id,result_id)',
               (issue, batch, scene, result_id, now()))
    # 场景归入问题时已有未结束的修复在处理它：一并关联，状态随该修复显示；已结束的旧修复不关联。
    db.execute("INSERT OR IGNORE INTO issue_fixes(issue,task) SELECT DISTINCT ?,t.id FROM fix_problems p JOIN tasks t ON t.id=p.task "
               "JOIN issues i ON i.id=? WHERE t.workspace=i.workspace AND t.kind='fix' AND p.batch=? AND p.scenario=? "
               f"AND t.status NOT IN ({sql_states(FIX_TERMINAL)})", (issue, issue, batch, scene))


def update(db, workspace, number, *, by, note, state=None, title=None, kind=None, root_cause=None, jira=None,
           add_scenarios=(), remove_scenarios=()):
    """改状态、标题、类型、根因、Jira 或影响场景；每次修改都登记一条带原话的决定。"""
    row = find(db, workspace, number)
    if not isinstance(note, str) or not note.strip():
        raise ValueError('请用 --note 写明这次决定的原话或理由')
    changes = {}
    if state is not None:
        if state not in MANUAL:
            raise ValueError('人只能把问题设为 ' + '、'.join(f'{s}（{LABELS[s]}）' for s in MANUAL))
        changes['status'] = state
    if kind is not None:
        if kind not in KINDS:
            raise ValueError('类型应为 ' + '、'.join(KINDS))
        changes['kind'] = kind
    for name, value in (('title', title), ('root_cause', root_cause), ('jira', jira)):
        if value is not None:
            changes[name] = value.strip()
    if 'title' in changes and not changes['title']:
        raise ValueError('问题标题不能为空')
    added, removed = sorted(set(add_scenarios)), sorted(set(remove_scenarios))
    check_scenarios(db, workspace, added)
    if changes:
        db.execute(f"UPDATE issues SET {','.join(k + '=?' for k in changes)},updated_at=? WHERE id=?",
                   (*changes.values(), now(), row['id']))
    for batch, scene in added:
        link(db, row['id'], batch, scene)
    for batch, scene in removed:
        db.execute('DELETE FROM issue_scenarios WHERE issue=? AND batch=? AND scenario=?', (row['id'], batch, scene))
    if not changes and not added and not removed:
        raise ValueError('没有要修改的内容；只登记决定请用 sop-board decision add --issue')
    shown = dict(status=LABELS, kind=KIND_LABELS)
    names = dict(status='状态', title='标题', kind='类型', root_cause='根因', jira='Jira')
    summary = [f'{names[name]}：{shown.get(name, {}).get(value, value)}' for name, value in changes.items()] + \
              [f'加入 {b}#{s}' for b, s in added] + [f'移出 {b}#{s}' for b, s in removed]
    record_decision(db, workspace, text=note.strip() + '（' + '；'.join(summary) + '）', author=by, source='issue', issue=row['id'])


def link_results(db, workspace, batch, number, rows):
    """执行结果行带 issue 编号时，把该场景与本轮结果归并到问题。"""
    for row in rows:
        if row.get('issue') is None:
            continue
        issue = find(db, workspace, row['issue'])
        result = db.execute('SELECT r.id FROM results r JOIN rounds t ON t.id=r.round_id WHERE t.batch=? AND t.number=? AND r.scenario=?',
                            (batch['id'], number, row['scenario'])).fetchone()
        link(db, issue['id'], batch['name'], row['scenario'], result['id'] if result else None)


def check_result_issues(db, workspace, rows):
    unknown = sorted({row['issue'] for row in rows if row.get('issue') is not None and not db.execute(
        'SELECT 1 FROM issues WHERE workspace=? AND number=?', (workspace, row['issue'])).fetchone()})
    if unknown:
        raise ValueError('结果行的 issue 不在问题台账：' + '、'.join(map(str, unknown)) + '；先用 sop-board issue add 新建')


def scenarios_of(db, workspace, numbers):
    pairs = set()
    for number in numbers:
        issue = find(db, workspace, number)
        pairs |= {(r['batch'], r['scenario']) for r in db.execute('SELECT batch,scenario FROM issue_scenarios WHERE issue=?', (issue['id'],))}
    return pairs


def link_fix(db, workspace, task_id, pairs, numbers=()):
    """修复任务纳入的场景属于哪些问题，就关联到哪些问题；--issues 指定的问题也关联。"""
    issues = {find(db, workspace, number)['id'] for number in numbers}
    for batch, scene in pairs:
        issues |= {r['issue'] for r in db.execute('SELECT s.issue FROM issue_scenarios s JOIN issues i ON i.id=s.issue '
                                                  'WHERE i.workspace=? AND s.batch=? AND s.scenario=?', (workspace, batch, scene))}
    for issue in issues:
        db.execute('INSERT OR IGNORE INTO issue_fixes(issue,task) VALUES (?,?)', (issue, task_id))
    return issues


def latest_results(db, workspace, pairs):
    latest = {}
    for batch, scene in pairs:
        row = db.execute("SELECT r.status,t.number FROM results r JOIN rounds t ON t.id=r.round_id JOIN batches b ON b.id=t.batch "
                         "WHERE b.workspace=? AND b.name=? AND r.scenario=? ORDER BY t.number DESC LIMIT 1", (workspace, batch, scene)).fetchone()
        latest[(batch, scene)] = dict(row) if row else None
    return latest


def items(db, workspace, *, batch=None, number=None, open_only=False):
    query = 'SELECT * FROM issues WHERE workspace=?' + (' AND number=?' if number is not None else '') + ' ORDER BY number DESC'
    result = []
    for row in db.execute(query, (workspace, number) if number is not None else (workspace,)).fetchall():
        pairs = [(r['batch'], r['scenario']) for r in db.execute('SELECT batch,scenario FROM issue_scenarios WHERE issue=? ORDER BY batch,scenario', (row['id'],))]
        if batch and pairs and all(b != batch for b, _ in pairs):
            continue
        fixes = [dict(r) for r in db.execute('SELECT t.id,t.status FROM issue_fixes f JOIN tasks t ON t.id=f.task WHERE f.issue=? ORDER BY t.id', (row['id'],))]
        shown = status(row['status'], fixes[-1]['status'] if fixes else None)
        if open_only and shown in ('closed', 'deferred'):
            continue
        latest = latest_results(db, workspace, pairs)
        scenarios = [dict(batch=b, scenario=s, latest=(latest[(b, s)] or {}).get('status')) for b, s in pairs]
        decisions = [dict(r, source=SOURCES.get(r['source'], r['source'])) for r in db.execute(
            'SELECT text,author,at,source FROM decisions WHERE issue=? ORDER BY id', (row['id'],))]
        result.append(dict(number=row['number'], title=row['title'], kind=row['kind'], kind_label=KIND_LABELS[row['kind']],
                           root_cause=row['root_cause'], status=shown, status_label=LABELS[shown], stored_status=row['status'],
                           jira=row['jira'], scenarios=scenarios, failing=sum(s['latest'] in ('fail', 'blocked') for s in scenarios),
                           fixes=fixes, decisions=decisions, created_at=row['created_at'], created_by=row['created_by'],
                           updated_at=row['updated_at']))
    return result


def blocking_issues(db, result_ids):
    """受阻结果按明确归入的未关闭问题归并（结果行带 issue 时登记），供连续运行的停止规则使用；返回 {结果编号: 问题说明}。"""
    owners = {}
    for result_id in result_ids:
        row = db.execute("SELECT i.number,i.title FROM issue_scenarios s JOIN issues i ON i.id=s.issue WHERE s.result_id=? "
                         "AND i.status!='closed' ORDER BY i.number LIMIT 1", (result_id,)).fetchone()
        if row:
            owners[result_id] = f"问题 {row['number']}：{row['title']}"
    return owners


def common_issue(db, workspace, batch_name, scenes):
    """这些场景都属于的同一个未关闭问题（取编号最小的）；没有时返回空字符串。"""
    shared = None
    for scene in scenes:
        numbers = {row['number']: row['title'] for row in db.execute(
            "SELECT i.number,i.title FROM issue_scenarios s JOIN issues i ON i.id=s.issue WHERE i.workspace=? AND s.batch=? "
            "AND s.scenario=? AND i.status!='closed'", (workspace, batch_name, scene))}
        shared = numbers if shared is None else {n: t for n, t in shared.items() if n in numbers}
        if not shared:
            return ''
    number = min(shared)
    return f'问题 {number}：{shared[number]}'


def decision_log(db, workspace, batch=None, limit=50):
    """决定记录：会话登记的决定、问题台账的修改、界面对照的决定与合并答复，按时间倒序。"""
    # 指定批次时：本批次的决定、不属于任何批次的工作区决定，以及影响本批次场景（或没有关联场景）的问题的决定。
    scoped = (" AND (d.batch=? OR d.batch IS NULL AND (d.issue IS NULL OR NOT EXISTS (SELECT 1 FROM issue_scenarios s WHERE s.issue=d.issue)"
              " OR EXISTS (SELECT 1 FROM issue_scenarios s WHERE s.issue=d.issue AND s.batch=?)))")
    entries = [dict(at=r['at'], author=r['author'], text=r['text'], source=SOURCES.get(r['source'], r['source']), batch=r['batch'],
                    issue=r['number']) for r in db.execute(
        'SELECT d.*,i.number FROM decisions d LEFT JOIN issues i ON i.id=d.issue WHERE d.workspace=?' + (scoped if batch else ''),
        (workspace, batch, batch) if batch else (workspace,))]
    filters = ' AND b.name=?' if batch else ''
    args = (workspace, batch) if batch else (workspace,)
    entries += [dict(at=r['decided_at'], author=r['decided_by'], text=f"{r['scenario']} 的“{r['text']}”："
                     + ('按 SOP' if r['decision'] == 'sop' else '改 SOP') + (('，' + r['decision_note']) if r['decision_note'] else ''),
                     source=SOURCES['atlas'], batch=r['name'], issue=None) for r in db.execute(
        'SELECT c.*,b.name FROM atlas_checks c JOIN batches b ON b.id=c.batch WHERE b.workspace=? AND c.decided_at IS NOT NULL' + filters, args)]
    entries += [dict(at=r['created_at'], author=r['author'], text=(f"缺口 {r['gap']}：" if r['gap'] else '') + r['text'],
                     source=SOURCES['merge'], batch=r['name'], issue=None) for r in db.execute(
        "SELECT m.*,b.name FROM merge_messages m JOIN batches b ON b.id=m.batch WHERE b.workspace=? AND m.operation='reply'" + filters, args)]
    return sorted(entries, key=lambda e: e['at'] or '', reverse=True)[:limit]


def command(arguments):
    """sop-board issue list|show|add|update [工作区] …；sop-board decision add|list [工作区] …"""
    group, rest = arguments[0], arguments[1:]
    parser = argparse.ArgumentParser(prog='sop-board ' + group, description='问题台账' if group == 'issue' else '决定记录')
    sub = parser.add_subparsers(dest='action', required=True)
    actions = ('list', 'show', 'add', 'update') if group == 'issue' else ('add', 'list')
    for action in actions:
        child = sub.add_parser(action)
        child.add_argument('targets', nargs='*', help='[工作区]')
        child.add_argument('--data', type=Path)
        if group == 'issue':
            if action == 'list':
                child.add_argument('--batch')
                child.add_argument('--open', action='store_true', help='只列未关闭、未暂缓的问题')
            if action in ('show', 'update'):
                child.add_argument('--issue', type=int, required=True)
            if action == 'add':
                child.add_argument('--title', required=True)
                child.add_argument('--kind', required=True, help='product、scenario、environment 或 tool')
                child.add_argument('--scenario', nargs='+', default=[], help='批次#场景')
                child.add_argument('--root-cause', default='')
                child.add_argument('--note', default='', help='新建时的原话或理由')
            if action == 'update':
                child.add_argument('--status', help='open（待处理）、deferred（暂缓）或 closed（已关闭）')
                child.add_argument('--title')
                child.add_argument('--kind', help='product、scenario、environment 或 tool')
                child.add_argument('--root-cause')
                child.add_argument('--jira')
                child.add_argument('--add-scenario', nargs='+', default=[])
                child.add_argument('--remove-scenario', nargs='+', default=[])
                child.add_argument('--note', default='', help='这次决定的原话或理由（必填）')
            if action in ('add', 'update'):
                child.add_argument('--by', help='决定人姓名')
        else:
            child.add_argument('--batch')
            if action == 'add':
                child.add_argument('--issue', type=int)
                child.add_argument('--text', required=True, help='人给出的原话')
                child.add_argument('--by', help='决定人姓名')
    args = parser.parse_args(rest)
    app = None
    try:
        from .session import Session
        app = Session(args.targets[0] if args.targets else None, args.data)
        key = app.key
        if getattr(args, 'batch', None):
            with app.store.connection() as db:
                app.service.batch(db, key, args.batch)
        if group == 'issue' and args.action in ('list', 'show'):
            with app.store.connection() as db:
                found = items(db, key, batch=getattr(args, 'batch', None), number=getattr(args, 'issue', None),
                              open_only=getattr(args, 'open', False))
            if args.action == 'show' and not found:
                raise ValueError(f'问题 {args.issue} 不存在')
            result = dict(ok=True, issue=found[0]) if args.action == 'show' else dict(ok=True, issues=found)
        elif group == 'issue':
            by = app.person(args.by)
            with app.store.transaction() as db:
                if args.action == 'add':
                    number = add(db, key, title=args.title, kind=args.kind, by=by, scenarios=parse_scenarios(args.scenario),
                                 root_cause=args.root_cause, note=args.note)
                else:
                    number = args.issue
                    update(db, key, number, by=by, note=args.note, state=args.status, title=args.title, kind=args.kind,
                           root_cause=args.root_cause, jira=args.jira, add_scenarios=parse_scenarios(args.add_scenario),
                           remove_scenarios=parse_scenarios(args.remove_scenario))
                result = dict(ok=True, issue=items(db, key, number=number)[0])
        elif args.action == 'add':
            by = app.person(args.by)
            if not args.text.strip():
                raise ValueError('请写明人给出的原话')
            with app.store.transaction() as db:
                issue = find(db, key, args.issue)['id'] if args.issue else None
                record_decision(db, key, text=args.text, author=by, source='session', batch=args.batch, issue=issue)
            result = dict(ok=True)
        else:
            with app.store.connection() as db:
                result = dict(ok=True, decisions=decision_log(db, key, args.batch))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, sqlite3.Error) as error:
        result = dict(ok=False, problems=[dict(field=group, problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
