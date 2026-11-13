"""仅本机的设置接口；候选校验与磁盘读取共用 Project.read。"""
import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

from . import config, products, peers, scenarios, browsers
from .entries import identities
from .errors import APIError, reject
from .redaction import names, ENV_NAME
from .server import route, json_response
from .store import DB, VERSION, version_error
from .time_format import local_time

SECTIONS = {
    'repositories': ('repos', 'identities', 'scenarios', 'snapshot_exclude'),
    'verification': ('entries', 'skill'), 'environment': ('adapter',),
    'fixtures': ('fixtures',), 'agents': ('agents',),
    'shared': ('shared_database',), 'local': (),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(project):
    raw = project.config_path.read_bytes()
    try:
        value = config.strict_yaml(raw.decode())
    except (ValueError, yaml.YAMLError, UnicodeError) as error:
        reject(422, 'settings-invalid', 'config.yaml', '配置无法解析：' + str(error))
    if not isinstance(value, dict):
        reject(422, 'settings-invalid', 'config.yaml', '配置须为对象；请先修正文件格式')
    return raw, value


def invalid(problems):
    raise APIError(422, 'settings-invalid', problems)


def section_problems(values, section):
    fields = SECTIONS[section]
    return [p for p in values['problems'] if any(p['field'] == f or p['field'].startswith(f + '.') for f in fields)]


def occupants(app, *, query_fixes=True, fix_db=None, notices=None):
    result = {key: [] for key in SECTIONS}
    with app.store.local() as db:
        local_fixes = [dict(row) for row in db.execute('SELECT f.fix,c.name FROM local_fixes f JOIN checkouts c ON c.id=f.checkout WHERE f.product=?',
                                                       (app.store.product if app.project.shared and app.project.binding() else 'local',))]
        for row in db.execute("SELECT e.checkout,c.name,e.state FROM environments e JOIN checkouts c ON c.id=e.checkout WHERE e.state IN ('starting','ready')"):
            item = dict(kind='environment', checkout=row['name'], state=row['state'])
            for key in ('environment', 'repositories'):
                result[key].append(item)
        for table, condition, kind in (('local_work', "status='prepared'", 'work'), ('launches', "status='running'", 'launch')):
            for row in db.execute(f'SELECT w.id,w.kind,c.name FROM {table} w JOIN checkouts c ON c.id=w.checkout WHERE w.{condition}'):
                item = dict(kind=kind, id=row['id'], work_kind=row['kind'], checkout=row['name'])
                for key in ('repositories', 'shared'):
                    result[key].append(item)
        for row in db.execute("SELECT b.identity,c.name FROM browser_instances b LEFT JOIN checkouts c ON c.id=b.last_checkout "
                              "WHERE b.state IN ('starting','ready')"):
            result['verification'].append(dict(kind='browser', identity=row['identity'], checkout=row['name']))
    if local_fixes:
        try:
            if fix_db is None and not query_fixes:
                raise APIError(503, 'database-unreachable', [])
            with (nullcontext(fix_db) if fix_db is not None else app.store.records()) as db:
                fixes = [dict(kind='fix', id=row['id'], checkout=row['checkout'], state='open')
                         for row in db.execute("SELECT id,checkout FROM fixes WHERE product=? AND machine=? AND status='open'",
                                               (app.store.product, app.store.machine['id']))]
        except APIError:
            # 无法确认时允许修正连接；只有已确认 open 的修复才占用配置。
            fixes = []
            if notices is not None:
                notices.extend(dict(field='fixes', problem=f"共享库连不上，无法确认修复 #{row['fix']} 是否结束")
                               for row in local_fixes)
        for key in ('repositories', 'shared'):
            result[key].extend(fixes)
    return result


def locked(items):
    if items:
        def describe(item):
            checkout = f"检出 {item['checkout']}"
            if item['kind'] == 'environment':
                return checkout + ' 的环境正在运行'
            if item['kind'] == 'browser':
                return checkout + f" 的身份 {item['identity']} 的浏览器正在运行"
            if item['kind'] == 'fix':
                return f"修复 #{item['id']} 进行中（{checkout}）"
            if item['kind'] == 'launch':
                return f"启动 #{item['id']}（{item['work_kind']}）正在运行（{checkout}）"
            label = {'run': '运行', 'distill': '沉淀', 'merge': '合并', 'author': '编写', 'draft': '草稿', 'atlas': '交互地图'}.get(item['work_kind'], '工作')
            return f"{label} #{item['id']}（{item['work_kind']}）已准备，还没提交或放弃（{checkout}）"
        raise APIError(409, 'settings-locked', [dict(field='occupants', problem=describe(item)) for item in items], occupants=items)


def credential_declarations(value):
    """同名变量保留每个声明来源，身份的 account_env 也一并展示。"""
    result = []
    def add(name, source):
        if isinstance(name, str) and ENV_NAME.fullmatch(name):
            result.append(dict(name=name, present=bool(os.environ.get(name)), source=source))
    entries = value.get('entries', {})
    for entry, declaration in entries.items() if isinstance(entries, dict) else ():
        if not isinstance(declaration, dict):
            continue
        for name in names({entry: declaration}):
            add(name, dict(kind='entry', entry=entry, field=f'entries.{entry}.credential_env'))
        for index, identity in enumerate(declaration.get('identities', []) if isinstance(declaration.get('identities', []), list) else []):
            if isinstance(identity, dict):
                add(identity.get('account_env'), dict(kind='identity', entry=entry, identity=identity.get('id'),
                                                      field=f'entries.{entry}.identities[{index}].account_env'))
    fixtures = value.get('fixtures', {})
    if isinstance(fixtures, dict):
        for name in names({'fixtures': fixtures}):
            add(name, dict(kind='fixtures', field='fixtures.credential_env',
                           recipes=[r['name'] for r in fixtures.get('recipes', []) if isinstance(r, dict) and isinstance(r.get('name'), str)]
                           if isinstance(fixtures.get('recipes', []), list) else []))
    return result


def references(app):
    found = {}
    for checkout in app.checkouts():
        project = app.project
        if not project.scenario_repo or project.scenario_repo not in checkout.paths:
            continue
        scanned = scenarios.scan(checkout.paths[project.scenario_repo] / project.scenario_path, set(project.entries))
        for key, scene in scanned['scenarios'].items():
            for entry in scene['verify'] if isinstance(scene['verify'], list) else [scene['verify']]:
                found.setdefault(entry, set()).add(key)
    return {key: dict(count=len(items), scenarios=sorted(items)[:5]) for key, items in found.items()}


def read(app, *, query_fixes=True):
    app.reload()
    raw, value = load(app.project)
    values = app.project.read(value)
    notices = app.project.recovery_notices()
    use = occupants(app, query_fixes=query_fixes, notices=notices if query_fixes else None)
    sections = {key: dict(content={f: value[f] for f in fields if f in value},
                          problems=section_problems(values, key), occupants=use[key]) for key, fields in SECTIONS.items()}
    shared = sections['shared']['content'].get('shared_database', {})
    if isinstance(shared, dict):
        saved = 'password' in shared
        shared.pop('password', None)
    else:
        saved = False
    sections['shared'].update(password_saved=saved, product=app.project.binding())
    sections['local']['content'] = dict(operator=config.operator(),
        operator_source='operator' if config.operator_path().is_file() else 'git config user.name',
        machine=app.store.machine['name'], runtime=browsers.runtime_info(), install_command='sop-board browser install',
        local_address=f'http://127.0.0.1:{app.port}', lan_addresses=app.addresses, data=str(app.project.data))
    pending = getattr(app, 'machine_name_sync_pending', False) or bool(app.project.shared and not app.registered)
    sections['local']['content']['machine_name_sync_pending'] = pending
    if pending:
        sections['local']['problems'].append(dict(field='machine', problem='业务库中的机器名尚未同步；下次工作台请求或看板重启后成功连库时同步'))
    skills = [dict(name=path.name, problems=section_problems(app.project.read(dict(value, skill=path.name)), 'verification'))
              for path in sorted((app.project.data / 'skills').glob('*')) if path.is_dir()]
    return dict(ok=True, digest=digest(raw), sections=sections, references=references(app), skills=skills,
                other_problems=[p for p in values['problems'] if not any(p in section['problems'] for section in sections.values())],
                notices=notices, credentials=credential_declarations(value))


def atomic(path, raw):
    """临时文件始终 600，避免含密码的旧内容在替换前短暂暴露。"""
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write(app, old, candidate, marker=None):
    project = app.project
    # 所有可能失败的解析与校验先完成，发布时不再做文件或 Git 读取。
    values = project.read(candidate)
    previous = project.config_path.with_name('config.yaml.prev')
    prior = previous.read_bytes() if previous.exists() else None
    original_marker = project.marker.read_bytes()
    try:
        atomic(previous, old)
        atomic(project.config_path, yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode())
        if marker is not None:
            atomic(project.marker, json.dumps(marker, ensure_ascii=False, indent=2).encode() + b'\n')
    except BaseException:
        atomic(project.config_path, old)
        atomic(project.marker, original_marker)
        if prior is None:
            previous.unlink(missing_ok=True)
        else:
            atomic(previous, prior)
        raise
    with project._publish:
        project.__dict__.update(values)
        project.update_saved_password()
    app.config_stamp = project.config_path.stat().st_mtime_ns


def current(app, request):
    raw, value = load(app.project)
    if request.json_field('digest') != digest(raw):
        reject(409, 'settings-stale', 'digest', '配置已被别处修改，请重新载入')
    return raw, value


def validate(app, before, candidate, section):
    old = app.project.read(before)
    after = app.project.read(candidate)
    original = {(p['field'], p['problem']) for p in old['problems']}
    problems = section_problems(after, section)
    problems += [p for p in after['problems'] if (p['field'], p['problem']) not in original and p not in problems]
    if problems:
        invalid(problems)


def repositories(app, before, content, refresh=None):
    repos = content.get('repos')
    if not isinstance(repos, (list, dict)):
        invalid([dict(field='repos', problem='须为仓库列表或别名到路径的对象')])
    registered = before.get('repos', {})
    result, origins = {}, {}
    rows = [dict(alias=a, path=p) for a, p in repos.items()] if isinstance(repos, dict) else repos
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            invalid([dict(field='repos', problem='每个仓库须含 path，可选 alias')])
        path = Path(row['path']).expanduser().resolve()
        existing = next((a for a, p in registered.items() if Path(p).expanduser().resolve() == path), None)
        alias = row.get('alias') or existing
        if not alias:
            base = re.sub('-+', '-', re.sub('[^a-z0-9-]+', '-', path.name.lower())).strip('-')
            if not re.match('[a-z]', base):
                base = 'r-' + base
            alias, n = base, 2
            while alias in result:
                alias, n = f'{base}-{n}', n + 1
        if existing and existing != alias:
            invalid([dict(field='repos.' + existing, problem='已登记仓库的别名不能改')])
        if alias in result:
            invalid([dict(field='repos', problem='仓库别名重复')])
        result[alias] = str(path)
        origins[alias] = (before.get('identities', {}).get(alias) if alias in registered and refresh != alias
                          else repository(path, app.project.data, alias))
    return dict(content, repos=result, identities=origins)


def repository(path, data, alias):
    try:
        return config.repository(path, data)
    except (ValueError, OSError) as error:
        invalid([dict(field=f'repos.{alias}', problem=str(error))])


@route('GET', '/api/settings', 'local')
def get_settings(app, request):
    with app.store.lock:
        return json_response(read(app))


@route('POST', r'/api/settings/sections/(?P<section>repositories|verification|environment|fixtures|agents)', 'local')
def save_section(app, request):
    section = request.match.group('section')
    with app.store.lock:
        raw, before = current(app, request)
        app.reload()
        content = request.json_field('content')
        if not isinstance(content, dict) or content.keys() - set(SECTIONS[section]):
            reject(422, 'settings-invalid', 'content', '须为这一节的配置对象，只能包含本节字段')
        owners = occupants(app)[section]
        if section != 'verification':
            locked(owners)
        else:
            if not isinstance(content.get('entries', {}), dict):
                reject(422, 'settings-invalid', 'entries', '须为入口名到配置的对象')
            remaining = {i for entry in content.get('entries', {}).values() if isinstance(entry, dict) for i in identities(entry)}
            locked([o for o in owners if o['identity'] not in remaining])
            removed = set(before.get('entries', {})) - set(content.get('entries', {}))
            refs = references(app)
            problems = [dict(field='entries.' + key, problem=f"有 {refs[key]['count']} 条场景引用：" + '、'.join(refs[key]['scenarios']))
                        for key in removed if key in refs]
            if problems:
                invalid(problems)
        if section == 'repositories':
            content = repositories(app, before, content)
        candidate = {key: value for key, value in before.items() if key not in SECTIONS[section]}
        candidate.update(content)
        validate(app, before, candidate, section)
        write(app, raw, candidate)
        return json_response(read(app))


@route('POST', '/api/settings/repositories/reregister', 'local')
def reregister(app, request):
    with app.store.lock:
        raw, before = current(app, request)
        locked(occupants(app)['repositories'])
        alias = request.json_field('alias')
        if alias not in before.get('repos', {}):
            reject(422, 'settings-invalid', 'alias', '仓库未登记')
        candidate = copy.deepcopy(before)
        candidate['identities'][alias] = repository(Path(candidate['repos'][alias]).expanduser().resolve(), app.project.data, alias)
        validate(app, before, candidate, 'repositories')
        write(app, raw, candidate)
        return json_response(read(app))


@route('POST', '/api/settings/local', 'local')
def save_local(app, request):
    name, operator = request.json_field('machine'), request.json_field('operator')
    for key, value in (('machine', name), ('operator', operator)):
        if not isinstance(value, str) or not value.strip() or len(value) > 64 or '\n' in value or '\r' in value:
            reject(422, 'settings-invalid', key, '须为非空单行文字，最长 64 字')
    with app.store.lock:
        load(app.project)
        app.reload()
        machine = app.store.machine
        changed = name.strip() != machine['name']
        config.set_operator(operator)
        atomic(app.project.data.parent / 'machine.json', json.dumps(dict(machine, name=name.strip()), ensure_ascii=False).encode())
        machine['name'] = name.strip()
        if changed:
            app.registered = False
            app.machine_name_sync_pending = True
    if changed:
        peers.ensure_registered(app)
    return json_response(read(app, query_fixes=False))


def connection(app, request):
    value = request.json_field('connection')
    if not isinstance(value, dict) or 'password' in value:
        reject(422, 'settings-invalid', 'connection', '须为 host、port、dbname、user 对象；密码用单独的 password 字段')
    if type(request.json_field('clear_password', False)) is not bool:
        reject(422, 'settings-invalid', 'clear_password', '须为布尔值')
    shared = dict(value)
    password = request.json_field('password')
    if password == '':
        reject(422, 'settings-invalid', 'password', '密码须为非空文字；不修改时省略 password，改用密码文件请使用 clear_password')
    if password is not None:
        shared['password'] = password
    elif not request.json_field('clear_password') and app.project.shared and 'password' in app.project.shared:
        shared['password'] = app.project.shared['password']
    problems = []
    result = config.validate_shared(shared, lambda field, problem: problems.append(dict(field=field, problem=problem)))
    if problems:
        invalid(problems)
    return result


@route('POST', '/api/settings/shared/test', 'local')
def test_shared(app, request):
    app.reload()
    shared = connection(app, request)
    conn, version = products.connect(shared)
    try:
        if version is not None and version != VERSION:
            raise version_error(version)
        items = products.listing(DB(conn, 'postgres', shared), products.local_scenes(app.project)) if version is not None else []
        for item in items:
            for field in ('created_at', 'latest_write'):
                if item[field]:
                    item[field] = local_time(item[field])
        return json_response(dict(ok=True, version=version, expected_version=VERSION, empty=not items,
                                  products=items, **products.bind_notes(app.project, app.store)))
    finally:
        conn.close()


@route('POST', '/api/settings/shared/connect', 'local')
def connect_shared(app, request):
    app.reload()
    # 占用查询需要当前业务库，目标连接可能指向另一个库；两边都在锁外建连接。
    if not app.project.shared:
        with app.store.records() as db:
            return _connect_shared(app, request, db)
    try:
        connection_, version = products.connect(app.project.shared)
    except APIError:
        connection_, version = None, None
    try:
        db = DB(connection_, 'postgres', app.project.shared) if version == VERSION else None
        return _connect_shared(app, request, db)
    finally:
        if connection_ is not None:
            connection_.close()


def _connect_shared(app, request, fix_db):
    # 连接建立可能耗时数秒；在锁外完成，随后持锁重新核对摘要和占用。
    with app.store.lock:
        current(app, request)
        app.reload()
        locked(occupants(app, query_fixes=False, fix_db=fix_db)['shared'])
        shared = connection(app, request)
    prepared = products.connect(shared)
    try:
        with app.store.lock:
            raw, before = current(app, request)
            app.reload()
            locked(occupants(app, query_fixes=False, fix_db=fix_db)['shared'])
            # 省略密码表示保留当前值；这次读出的摘要已重新核对。
            shared = connection(app, request)
            name = request.json_field('name')
            if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 64 or '\n' in name or '\r' in name):
                reject(422, 'settings-invalid', 'name', '产品名称须为非空单行文字，最长 64 字')
            action = request.json_field('action')
            if action not in ('join', 'new', 'keep'):
                reject(422, 'settings-invalid', 'action', '须选 join、new 或 keep')
            join = request.json_field('product_id')
            if action == 'keep':
                bound = app.project.binding()
                if not bound:
                    reject(409, 'product-unbound', 'product', '尚未绑定产品，请使用加入或新建')
                join = bound['id']
            if action != 'new' and (not isinstance(join, str) or not join):
                reject(422, 'settings-invalid', 'product_id', '加入时须指定产品 ID')
            candidate = dict(before, shared_database=shared)
            validate(app, before, candidate, 'shared')
            temporary = copy.copy(app.project)
            temporary.__dict__.update(temporary.read(candidate))
            original_marker = app.project.marker.read_bytes()
            prev = app.project.config_path.with_name('config.yaml.prev')
            original_prev = prev.read_bytes() if prev.exists() else None
            def persist(product):
                marker = json.loads(original_marker)
                marker['product'] = product
                write(app, raw, candidate, marker)
            try:
                result, code = products.init(temporary, join=join if action != 'new' else None,
                                             new=action == 'new', name=name, persist=persist, prepared=prepared)
                if code:
                    if action == 'keep':
                        reject(409, 'product-not-found', 'product', f'目标库没有已绑定产品 {join}；要换产品请用加入或新建')
                    raise APIError(409, result['error_code'], result['problems'])
            except BaseException:
                atomic(app.project.config_path, raw)
                atomic(app.project.marker, original_marker)
                if original_prev is None:
                    prev.unlink(missing_ok=True)
                else:
                    atomic(prev, original_prev)
                app.project.reload()
                raise
            if app.store.pool:
                app.store.pool.close()
                app.store.pool, app.store.pool_key = None, None
            app.registered = False
    finally:
        prepared[0].close()
    peers.ensure_registered(app)
    return json_response(read(app))


@route('POST', '/api/settings/shared/disconnect', 'local')
def disconnect_shared(app, request):
    with app.store.lock:
        raw, before = current(app, request)
        app.reload()
        locked(occupants(app)['shared'])
        peers.withdraw(app)
        marker = json.loads(app.project.marker.read_bytes())
        marker.pop('product', None)
        candidate = dict(before)
        candidate.pop('shared_database', None)
        write(app, raw, candidate, marker)
        if app.store.pool:
            app.store.pool.close()
            app.store.pool, app.store.pool_key = None, None
        app.registered = False
        peers.ensure_registered(app)
        return json_response(read(app))
