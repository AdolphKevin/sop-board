"""待人处理、身份使用计数与前置数据配方。"""
import json
from pathlib import Path
import re

from .entries import ENV_NAME

RECIPE_NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\0' not in value


def _texts(value, *, empty=False):
    return isinstance(value, list) and (empty or bool(value)) and all(_text(v) for v in value)


def validate_fixtures(value, problem):
    """fixtures：{environment, scope, credential_env?, adhoc?, recipes?, real_only?}。"""
    if not isinstance(value, dict):
        problem('fixtures', '须为含 environment、scope 的对象')
        return None
    for field in value.keys() - {'environment', 'scope', 'credential_env', 'adhoc', 'recipes', 'real_only'}:
        problem('fixtures.' + field, '格式外字段')
    if not _text(value.get('environment')):
        problem('fixtures.environment', '须为非空文字')
    if not _texts(value.get('scope')):
        problem('fixtures.scope', '须为非空文字数组')
    credentials = value.get('credential_env', [])
    if (not isinstance(credentials, list) or not all(isinstance(v, str) and ENV_NAME.fullmatch(v) for v in credentials)
            or len(set(credentials)) != len(credentials)):
        problem('fixtures.credential_env', '须为不重复的环境变量名数组')
        credentials = []
    if type(value.get('adhoc', False)) is not bool:
        problem('fixtures.adhoc', '须为布尔值')
    real_only = value.get('real_only', [])
    if not _texts(real_only, empty=True):
        problem('fixtures.real_only', '须为文字数组')
        real_only = []
    recipes, seen = value.get('recipes', []), set()
    if not isinstance(recipes, list):
        problem('fixtures.recipes', '须为数组')
        recipes = []
    for index, recipe in enumerate(recipes):
        prefix = f'fixtures.recipes[{index}]'
        if not isinstance(recipe, dict):
            problem(prefix, '配方须为对象')
            continue
        for field in recipe.keys() - {'name', 'title', 'params', 'creates', 'run', 'check'}:
            problem(f'{prefix}.{field}', '格式外字段')
        name = recipe.get('name')
        if not isinstance(name, str) or not RECIPE_NAME.fullmatch(name):
            problem(prefix + '.name', '须只含小写字母、数字与连字符，且以字母或数字开头')
        elif name in seen:
            problem(prefix + '.name', '配方名称不能重复')
        seen.add(name)
        if not _text(recipe.get('title')):
            problem(prefix + '.title', '须为非空文字')
        if not _texts(recipe.get('creates', []), empty=True):
            problem(prefix + '.creates', '须为文字数组')
        params = recipe.get('params', {})
        if (not isinstance(params, dict) or not all(ENV_NAME.fullmatch(k) and _text(v) for k, v in params.items())
                or len({k.upper() for k in params}) != len(params)):
            problem(prefix + '.params', '须为参数名到非空说明的对象；参数名须能转为互不重复的环境变量名')
        for field in ('run', 'check'):
            if (field == 'run' or field in recipe) and not _texts(recipe.get(field)):
                problem(f'{prefix}.{field}', '须为非空命令参数数组，不经过 shell')
    return dict(value, credential_env=credentials, adhoc=value.get('adhoc', False), recipes=recipes, real_only=real_only)


ATTENTION_KINDS = ('人机验证', 'OAuth 授权', '账号登录', '第三方后台设置', '权限放行', '邮箱验证',
                   '环境问题', '前置数据', '浏览器崩溃', '其他')


def open_attention(db, store, *, local):
    """产品内未解决的待人处理（业务记录库），每条附产出机器与检出。"""
    names = machine_names(db)
    rows = db.execute('SELECT * FROM attention WHERE product=? AND resolved_at IS NULL ORDER BY id', (store.product,))
    result = []
    for row in rows:
        item = dict(id=row['id'], kind=row['kind'], text=row['text'], scenarios=json.loads(row['scenarios']),
                    created_by=row['created_by'], created_at=row['created_at'],
                    machine=names.get(row['machine'], row['machine']), checkout=row['checkout'],
                    here=row['machine'] == store.machine['id'])
        if local:
            item['work'] = row['work']
        result.append(item)
    return result


def machine_names(db):
    """机器标识 → 显示名。"""
    return {row['id']: row['name'] for row in db.execute('SELECT id, name FROM machines')}


def insert_attention(db, store, checkout_name, kind, text, *, by, scenarios=(), work=None):
    from .store import dumps, now
    return db.insert('INSERT INTO attention(product,machine,checkout,kind,text,scenarios,work,created_by,created_at) '
                     'VALUES (?,?,?,?,?,?,?,?,?)',
                     (store.product, store.machine['id'], checkout_name, kind, text, dumps(list(scenarios)), work, by, now()))


# ---- 待人处理 ----

def attention_add(store, checkout_name, kind, text, *, by, scenarios=(), work=None):
    from .errors import reject
    if kind not in ATTENTION_KINDS:
        reject(422, 'attention-invalid', 'kind', '种类应为 ' + '、'.join(ATTENTION_KINDS))
    if not isinstance(text, str) or not text.strip():
        reject(422, 'attention-invalid', 'text', '内容不能为空')
    with store.records_tx() as db:
        number = insert_attention(db, store, checkout_name, kind, text.strip(), by=by, scenarios=scenarios, work=work)
    return dict(ok=True, attention=number)


def attention_resolve(store, item_id, *, by, note=None):
    from .errors import reject
    from .store import now
    with store.records_tx() as db:
        row = db.one('SELECT * FROM attention WHERE id=? AND product=?', (item_id, store.product))
        if row is None:
            reject(404, 'attention-not-found', 'attention', f'待人处理不存在：#{item_id}')
        # 条件更新：别的机器同时处理同一条时只有一次生效。
        if db.execute('UPDATE attention SET resolved_by=?, resolved_at=?, note=? WHERE id=? AND resolved_at IS NULL',
                      (by, now(), note, item_id)).rowcount == 0:
            return dict(ok=True, attention=item_id, duplicate=True)
    return dict(ok=True, attention=item_id, resolved_by=by)


# ---- 身份使用计数 ----

def identity_spec(project, identity):
    for name, entry in project.entries.items():
        if isinstance(entry, dict) and entry.get('surface') == 'web':
            for item in entry.get('identities') or []:
                if item['id'] == identity:
                    return name, item
    from .errors import reject
    reject(404, 'identity-not-found', 'identity', f'没有声明身份 {identity}')


def identity_counts(db, checkout_id, identity):
    from datetime import datetime, timedelta, timezone
    current = datetime.now(timezone.utc)
    day, hour = (current - timedelta(days=1)).isoformat(), (current - timedelta(hours=1)).isoformat()
    def count(kind, since):
        return db.execute('SELECT COUNT(*) FROM identity_events WHERE checkout=? AND identity=? AND kind=? AND recorded_at>=?',
                          (checkout_id, identity, kind, since)).fetchone()[0]
    return dict(logins_per_day=count('login', day), codes_per_hour=count('code', hour), codes_per_day=count('code', day))


def identity_record(project, store, checkout, identity, kind, *, by, work=None):
    """真实登录或请求验证码之前记一次；达到上限时拒绝并登记待人处理。"""
    from .errors import reject
    from .store import now
    if kind not in ('login', 'code'):
        reject(422, 'identity-invalid', 'kind', '应为 login 或 code')
    entry, spec = identity_spec(project, identity)
    limits = spec.get('limits', {})
    # 使用次数是本机状态（不跨机器合并）；到上限时登记的待人处理在业务记录库。
    with store.local_tx() as db:
        counts = identity_counts(db, checkout.id, identity)
        keys = ('logins_per_day',) if kind == 'login' else ('codes_per_hour', 'codes_per_day')
        over = [key for key in keys if key in limits and counts[key] >= limits[key]]
        if not over:
            db.execute('INSERT INTO identity_events(checkout,entry,identity,kind,work,recorded_at) VALUES (?,?,?,?,?,?)',
                       (checkout.id, entry, identity, kind, work, now()))
    if over:
        label = {'logins_per_day': '每天登录', 'codes_per_hour': '每小时验证码', 'codes_per_day': '每天验证码'}
        text = f"身份 {identity} 的{'、'.join(label[k] for k in over)}次数已达上限（" + \
               '、'.join(f'{counts[k]}/{limits[k]}' for k in over) + '），需要人处理后再继续'
        with store.records_tx() as db:
            insert_attention(db, store, checkout.name, '账号登录' if kind == 'login' else '邮箱验证', text, by=by, work=work)
        reject(409, 'identity-limit', 'identity', text)
    with store.local() as db:
        return dict(ok=True, identity=identity, kind=kind, counts=identity_counts(db, checkout.id, identity), limits=limits)


def identity_status(project, store, checkout):
    result = []
    with store.local() as db:
        for name, entry in project.entries.items():
            if isinstance(entry, dict) and entry.get('surface') == 'web':
                for item in entry.get('identities') or []:
                    result.append(dict(entry=name, identity=item['id'], label=item['label'], real=item.get('real', False),
                                       counts=identity_counts(db, checkout.id, item['id']), limits=item.get('limits', {})))
    return result


# ---- 前置数据 ----

def fixture_run(project, store, checkout, *, recipe=None, argv=None, reason=None, params=(), run=None, scenario=None, by):
    """执行配方（或在 adhoc 允许时执行临时命令），留痕写进 fixture_runs 与运行目录；凭据取值遮蔽。"""
    import os
    import subprocess
    from .errors import reject
    from .redaction import redact
    from .store import dumps, now
    declared = project.fixtures
    if declared is None:
        reject(409, 'fixtures-not-declared', 'fixtures', '项目没有声明 fixtures')
    values = {}
    for item in params:
        name, separator, value = item.partition('=')
        if not separator or not name:
            reject(422, 'fixture-invalid', 'param', '参数写成 名=值')
        values[name] = value
    if recipe:
        found = next((r for r in declared['recipes'] if r['name'] == recipe), None)
        if found is None:
            reject(404, 'recipe-not-found', 'recipe', f'没有配方 {recipe}')
        unknown = set(values) - set(found.get('params', {}))
        if unknown:
            reject(422, 'fixture-invalid', 'param', '配方没有这些参数：' + '、'.join(sorted(unknown)))
        command, kind, name = list(found['run']), 'recipe', recipe
    else:
        if not declared.get('adhoc'):
            reject(409, 'adhoc-not-allowed', 'fixtures', '项目没有允许临时写入（fixtures.adhoc），只能使用配方')
        if not argv or not reason:
            reject(422, 'fixture-invalid', 'command', '临时写入须写明 --reason 并在 -- 之后给出命令')
        command, kind, name = list(argv), 'adhoc', None
    executable = project.data / command[0]
    if executable.is_file():
        command[0] = str(executable)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SOP_')}
    env.update({key.upper(): value for key, value in values.items()})
    env.update(SOP_CHECKOUT=checkout.name, **{'SOP_REPO_' + a.upper().replace('-', '_'): str(p) for a, p in checkout.paths.items()})
    secrets = [os.environ[n] for n in declared.get('credential_env', []) if os.environ.get(n)]
    started = now()
    result = subprocess.run(command, cwd=project.data, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
    output = redact(result.stdout + result.stderr, secrets).decode('utf-8', errors='replace')[-20000:]
    check_output = None
    if kind == 'recipe' and found.get('check') and result.returncode == 0:
        check = subprocess.run(list(found['check']), cwd=project.data, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        check_output = redact(check.stdout + check.stderr, secrets).decode('utf-8', errors='replace')[-8000:]
    record = dict(kind=kind, name=name, reason=reason, scenario=scenario, params=values, command=command, exit_code=result.returncode,
                  output=output, check=check_output, operator=by, started_at=started, finished_at=now())
    shown = json.loads(redact(json.dumps(record, ensure_ascii=False).encode(), secrets))
    with store.local_tx() as db:
        cursor = db.execute('INSERT INTO fixture_runs(work,kind,name,scenario,params,command,exit_code,output,operator,started_at,finished_at) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (run, kind, name, scenario, dumps(shown['params']), dumps(shown['command']), result.returncode,
                             shown['output'], by, started, shown['finished_at']))
        number = cursor.lastrowid
        if run:
            row = db.execute('SELECT dir FROM local_work WHERE id=?', (run,)).fetchone()
            if row and row['dir']:
                directory = Path(row['dir']) / 'fixtures'
                directory.mkdir(exist_ok=True)
                (directory / f'{number}.json').write_text(json.dumps(shown, ensure_ascii=False, indent=2), encoding='utf-8')
    if result.returncode:
        reject(422, 'fixture-failed', 'command', f'配方执行失败（退出码 {result.returncode}）：{shown["output"][-300:]}')
    return dict(ok=True, fixture_run=number, **{k: shown[k] for k in ('kind', 'name', 'exit_code', 'check')})
