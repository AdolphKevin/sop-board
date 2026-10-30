"""待人处理队列（D94）：人机验证、OAuth、第三方后台设置、权限放行、邮箱验证、环境问题等需要人做的事。

执行者、连续运行或人都可以登记；处理后须填写姓名标记“已处理”，受影响的场景随下一轮计划重新排入。
"""
import argparse
import json
from pathlib import Path
import sqlite3

from .errors import APIError
from .service import canonical, now

KINDS = ('captcha', 'oauth', 'account', 'setting', 'permission', 'mailbox', 'environment', 'data', 'run-paused', 'other')
LABELS = dict(captcha='人机验证', oauth='OAuth 授权', account='账号登录', setting='第三方后台设置', permission='权限放行',
              mailbox='邮箱验证', environment='环境问题', data='前置数据', other='其他')
LABELS['run-paused'] = '连续运行暂停'


def add(store, workspace, *, kind, text, batch=None, scenarios=(), task=None, created_by=''):
    if kind not in KINDS:
        raise ValueError('类型应为 ' + '、'.join(KINDS))
    if not isinstance(text, str) or not text.strip():
        raise ValueError('请写明需要人做的事')
    with store.transaction() as db:
        return db.execute('INSERT INTO attention_items(workspace,batch,kind,scenarios_json,text,task,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)',
                          (workspace, batch, kind, canonical(sorted(set(scenarios))), text.strip(), task, now(), created_by or '')).lastrowid


def items(db, workspace, *, open_only=True, batch=None):
    query = 'SELECT * FROM attention_items WHERE workspace=?' + (' AND resolved_at IS NULL' if open_only else '') + \
            (' AND (batch=? OR batch IS NULL)' if batch else '') + ' ORDER BY id DESC'
    rows = [dict(r) for r in db.execute(query, (workspace, batch) if batch else (workspace,))]
    for row in rows:
        row['scenarios'] = json.loads(row.pop('scenarios_json'))
        row['label'] = LABELS.get(row['kind'], row['kind'])
    return rows


def open_scenarios(db, workspace, batch):
    return {scenario for item in items(db, workspace, batch=batch) if item['batch'] == batch for scenario in item['scenarios']}


def resolve(store, workspace, item_id, by, note=''):
    if not isinstance(by, str) or not by.strip():
        raise ValueError('请填写处理人姓名')
    with store.transaction() as db:
        row = db.execute('SELECT id FROM attention_items WHERE id=? AND workspace=? AND resolved_at IS NULL', (item_id, workspace)).fetchone()
        if row is None:
            raise ValueError('事项不存在或已处理')
        db.execute('UPDATE attention_items SET resolved_at=?,resolved_by=?,resolution_note=? WHERE id=?', (now(), by.strip(), note or '', item_id))


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board attention', description='待人处理事项')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('list', 'add', 'resolve'):
        child = sub.add_parser(action)
        child.add_argument('targets', nargs='*', help='[工作区]')
        child.add_argument('--data', type=Path)
        if action == 'list':
            child.add_argument('--batch')
            child.add_argument('--all', action='store_true', help='包括已处理的事项')
        elif action == 'add':
            child.add_argument('--kind', required=True, choices=KINDS)
            child.add_argument('--text', required=True)
            child.add_argument('--batch')
            child.add_argument('--scenario', action='append', default=[])
            child.add_argument('--task', type=int)
            child.add_argument('--session', required=True, help='登记的会话或执行者标识')
        else:
            child.add_argument('--item', type=int, required=True)
            child.add_argument('--by', help='处理人姓名')
            child.add_argument('--note', default='')
    args = parser.parse_args(arguments)
    app = None
    try:
        from .session import Session
        app = Session(args.targets[0] if args.targets else None, args.data)
        key = app.key
        if args.action == 'list':
            with app.store.connection() as db:
                result = dict(ok=True, items=items(db, key, open_only=not args.all, batch=args.batch))
        elif args.action == 'add':
            if args.batch:
                with app.store.connection() as db:
                    app.service.batch(db, key, args.batch)
            item = add(app.store, key, kind=args.kind, text=args.text, batch=args.batch, scenarios=args.scenario, task=args.task,
                       created_by=args.session)
            result = dict(ok=True, item=item)
        else:
            resolve(app.store, key, args.item, app.person(args.by), args.note)
            result = dict(ok=True, item=args.item)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, sqlite3.Error) as error:
        result = dict(ok=False, problems=[dict(field='attention', problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
