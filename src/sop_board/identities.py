"""身份池（D95）：Web 入口声明身份与各自的浏览器、验证码方式和次数上限，看板计数并在接近上限时提醒。

每个身份用自己的浏览器保持登录，切换身份就是换浏览器，不退出、不在同一浏览器里换账号。
执行者在每次真实登录或请求验证码前运行 `sop-board identity record`；超出上限时被拒并登记待人处理。
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3

from .errors import APIError
from .service import canonical, now

IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
ENV = re.compile(r'[A-Z_][A-Z0-9_]*\Z')
CODES = ('mailbox', 'backend', 'manual')
LIMITS = ('logins_per_day', 'codes_per_hour', 'codes_per_day')


def validate(value, field, problems):
    """校验 entries.<入口>.identities；只用于 Web 入口。"""
    if not isinstance(value, list) or not value:
        problems.append(dict(field=field, problem='须为非空身份数组'))
        return
    seen = set()
    for index, item in enumerate(value):
        where = f'{field}[{index}]'
        if not isinstance(item, dict):
            problems.append(dict(field=where, problem='身份须为对象'))
            continue
        for extra in item.keys() - {'id', 'label', 'account_env', 'cdp', 'code', 'limits', 'real_once'}:
            problems.append(dict(field=f'{where}.{extra}', problem='格式外字段'))
        if not isinstance(item.get('id'), str) or not IDENTIFIER.fullmatch(item['id']) or item['id'] in seen:
            problems.append(dict(field=where + '.id', problem='须为不重复的小写编号'))
        seen.add(item.get('id'))
        if not isinstance(item.get('label'), str) or not item['label'].strip():
            problems.append(dict(field=where + '.label', problem='须为非空文字，对应场景的 actor'))
        if 'account_env' in item and (not isinstance(item['account_env'], str) or not ENV.fullmatch(item['account_env'])):
            problems.append(dict(field=where + '.account_env', problem='须为环境变量名'))
        if 'cdp' in item and (not isinstance(item['cdp'], str) or not re.match(r'(https?|wss?)://', item['cdp'])):
            problems.append(dict(field=where + '.cdp', problem='须为浏览器调试地址，如 http://127.0.0.1:9231'))
        if item.get('code', 'manual') not in CODES:
            problems.append(dict(field=where + '.code', problem='应为 ' + '、'.join(CODES)))
        limits = item.get('limits', {})
        if not isinstance(limits, dict) or limits.keys() - set(LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
            problems.append(dict(field=where + '.limits', problem='只接受 ' + '、'.join(LIMITS) + '，取值为正整数'))
        if 'real_once' in item and type(item['real_once']) is not bool:
            problems.append(dict(field=where + '.real_once', problem='须为布尔值'))


def declared(workspace):
    return {name: entry['identities'] for name, entry in workspace.entries.items()
            if isinstance(entry, dict) and entry.get('surface') == 'web' and isinstance(entry.get('identities'), list)}


def counts(db, workspace, entry, identity):
    current = datetime.now(timezone.utc)
    day, hour = (current - timedelta(days=1)).isoformat(), (current - timedelta(hours=1)).isoformat()
    row = db.execute("SELECT SUM(kind='login' AND at>=?) AS logins_per_day, SUM(kind='code' AND at>=?) AS codes_per_hour, "
                     "SUM(kind='code' AND at>=?) AS codes_per_day FROM identity_events WHERE workspace=? AND entry=? AND identity=?",
                     (day, hour, day, workspace, entry, identity)).fetchone()
    return {key: row[key] or 0 for key in LIMITS}


def find(workspace, entry, identity):
    found = next((item for item in declared(workspace).get(entry, []) if item.get('id') == identity), None)
    if found is None:
        raise ValueError(f'入口 {entry} 没有声明身份 {identity}')
    return found


def record(store, workspace, entry, identity, event, task=None):
    """登记一次登录或验证码请求；超出上限时拒绝并登记待人处理，接近上限（八成）时提醒。"""
    if event not in ('login', 'code'):
        raise ValueError('--event 应为 login 或 code')
    spec = find(workspace, entry, identity)
    limits = spec.get('limits', {})
    with store.transaction() as db:
        current = counts(db, workspace.name, entry, identity)
        keys = ['logins_per_day'] if event == 'login' else ['codes_per_hour', 'codes_per_day']
        exceeded = [key for key in keys if key in limits and current[key] + 1 > limits[key]]
        if not exceeded:
            db.execute('INSERT INTO identity_events(workspace,entry,identity,kind,at,task) VALUES (?,?,?,?,?,?)',
                       (workspace.name, entry, identity, event, now(), task))
            current = counts(db, workspace.name, entry, identity)
    if exceeded:
        from .attention import add
        text = f"身份 {identity}（{spec['label']}）的{'、'.join(exceeded)} 已达上限，暂停用它{'登录' if event == 'login' else '请求验证码'}"
        add(store, workspace.name, kind='account', text=text, task=task, created_by='身份池')
        raise ValueError(text)
    warnings = [f'{key} 已用 {current[key]}/{limits[key]}' for key in keys if key in limits and current[key] >= limits[key] * 0.8]
    return dict(identity=identity, event=event, counts=current, limits=limits, warnings=warnings)


def materialize(workspace, store, folder, entries_used):
    """执行任务目录 identities.json：身份声明（只含变量名）与当前计数。"""
    pools = {name: items for name, items in declared(workspace).items() if name in entries_used}
    if not pools:
        return False
    with store.connection() as db:
        payload = {name: [dict(item, counts=counts(db, workspace.name, name, item['id'])) for item in items] for name, items in pools.items()}
    (Path(folder) / 'identities.json').write_text(canonical(dict(entries=payload)), encoding='utf-8')
    return True


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board identity', description='身份池：登录与验证码计数')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('record', 'status'):
        child = sub.add_parser(action)
        child.add_argument('targets', nargs='*', help='[工作区]')
        child.add_argument('--data', type=Path)
        child.add_argument('--entry', required=action == 'record')
        if action == 'record':
            child.add_argument('--identity', required=True)
            child.add_argument('--event', required=True, choices=('login', 'code'))
            child.add_argument('--task', type=int)
    args = parser.parse_args(arguments)
    app = None
    try:
        from .session import Session
        app = Session(args.targets[0] if args.targets else None, args.data)
        app.configured()
        if args.action == 'record':
            result = dict(ok=True, **record(app.store, app.workspace, args.entry, args.identity, args.event, args.task))
        else:
            pools = declared(app.workspace)
            if args.entry:
                pools = {args.entry: pools.get(args.entry, [])}
            with app.store.connection() as db:
                result = dict(ok=True, entries={name: [dict(id=i['id'], label=i['label'], code=i.get('code', 'manual'), limits=i.get('limits', {}),
                    counts=counts(db, app.key, name, i['id'])) for i in items] for name, items in pools.items()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, sqlite3.Error) as error:
        result = dict(ok=False, problems=[dict(field='identity', problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
