"""产品与加入（已定决策 21）：共享库按产品登记，每个产品有 ID 和名称；本机项目绑定到一个产品，绑定关系记在本机 project.json。

`init <项目>` 只列出产品，以 product-choice-required 结束，不自动绑定：是不是同一个产品由 Agent 问使用者。
新建、加入和共享都不读取、不比对任何仓库的 origin，不扫描目录，不往产品仓库写东西。
"""
import uuid

from . import config, scenarios, store as records
from .errors import APIError


def require_shared(project):
    if project.shared_invalid:
        problems = [p for p in project.problems if p['field'].startswith('shared_database')]
        raise APIError(422, 'shared-database-invalid', problems)
    if not project.shared:
        raise APIError(409, 'shared-database-required', [dict(field='shared_database', problem=(
            f'项目 {project.name} 的 config.yaml 没有写 shared_database：没配共享库时项目自己就是产品，不需要选择产品。'
            '多人共享验收记录时，先在 config.yaml 写 shared_database（host、port、dbname、user，password 可选，或由 ~/.pgpass 提供），再运行这条命令'))])
    return project.shared


def connect(shared):
    """连接共享库并读出表结构版本：空库返回 (连接, None)。"""
    connection = records.pg_connect(shared)
    try:
        return connection, records.pg_version(connection)
    except Exception:
        connection.close()
        raise


def local_scenes(project):
    """本机 main 检出的场景（键 → 指纹），用来给出“本机 N 条场景中，有 M 条在该产品里已有结果”的参考。"""
    if not project.configured or not project.scenario_repo:
        return {}
    directory = project.repos[project.scenario_repo] / project.scenario_path
    found = scenarios.scan(directory, set(project.entries))
    return {key: scene['fingerprint'] for key, scene in found['scenarios'].items() if scene.get('status') != 'draft'}


def listing(db, scenes):
    """共享库里的产品：名称、创建人、有结果的场景数、最近写入时间，以及本机场景与它的重合条数。"""
    items = []
    for product in db.all('SELECT * FROM products ORDER BY created_at'):
        keys = db.all('SELECT DISTINCT file, scenario, fingerprint FROM results WHERE product=?', (product['id'],))
        with_results = {scenarios.key(r['file'], r['scenario']) for r in keys}
        overlap = {scenarios.key(r['file'], r['scenario']) for r in keys if scenes.get(scenarios.key(r['file'], r['scenario'])) == r['fingerprint']}
        latest = max([value for value in (
            db.value('SELECT MAX(ended_at) FROM work WHERE product=?', (product['id'],)),
            db.value("SELECT MAX(executed_at) FROM results WHERE product=? AND kind='manual'", (product['id'],)),
            db.value('SELECT MAX(ran_at) FROM guard_runs WHERE product=?', (product['id'],)),
            db.value('SELECT MAX(created_at) FROM annotations WHERE product=?', (product['id'],)),
            db.value('SELECT MAX(created_at) FROM inbox WHERE product=?', (product['id'],))) if value], default=None)
        items.append(dict(id=product['id'], name=product['name'], created_by=product['created_by'],
                          created_at=product['created_at'], scenarios_with_results=len(with_results), latest_write=latest,
                          local_overlap=len(overlap),
                          hint=f'本机 {len(scenes)} 条场景中，有 {len(overlap)} 条在该产品里已有结果（内容一致）'))
    return items


def bind_notes(project, store):
    """绑定时的两条提示：本机已有的结果不带入共享库；regression.yaml 引用、但本机没有登记的仓库别名。"""
    with store.local() as db:
        local = db.execute("SELECT COUNT(*) FROM results WHERE product='local'").fetchone()[0]
    notes = [f'本机已有的 {local} 条结果不带入共享库；已登记的守护会因验收结果不在共享库而失效，须在共享库里重新验收后再沉淀']
    absent = []
    if project.configured and project.scenario_repo:
        from .guards import load_index
        from .scenarios import GUARD_INDEX
        try:
            index = load_index(project.repos[project.scenario_repo] / project.scenario_path / GUARD_INDEX)
        except ValueError:
            index = dict(commands={}, guards={})
        for command in index['commands'].values():
            if command.get('repo') and command['repo'] not in project.repos and command['repo'] not in absent:
                absent.append(command['repo'])
        for guard in index['guards'].values():
            for test in guard['tests']:
                if test.get('repo') and test['repo'] not in project.repos and test['repo'] not in absent:
                    absent.append(test['repo'])
    if absent:
        notes.append('regression.yaml 引用、但本机没有登记的仓库别名：' + '、'.join(absent)
                     + '；引用它们的守护在本机列为“本机无法运行”')
    return dict(local_results=local, unregistered_aliases=absent, notes=notes)


def init(project, *, join=None, new=False, name=None, persist=None, prepared=None):
    """返回 (输出, 退出码)。"""
    shared = require_shared(project)
    store = records.Store(project)
    scenes = local_scenes(project)
    notes = bind_notes(project, store)
    connection, version = prepared if prepared is not None else connect(shared)
    try:
        if version is not None and version != records.VERSION:
            raise records.version_error(version)
        db = records.DB(connection, 'postgres', shared)
        current = project.binding()
        if new:
            product = dict(id='p' + uuid.uuid4().hex[:11], name=(name or project.name).strip())
            if not product['name'] or len(product['name']) > 64 or '\n' in product['name']:
                return dict(ok=False, error_code='invalid-arguments', problems=[dict(field='product', problem='产品名称须为一行文字，最长 64 字')]), 2
            machine = store.machine
            with connection.transaction():
                if version is None:
                    # 空库：按当前版本建表。并发的另一台机器先建好时，再核对一次版本。
                    connection.execute('SELECT pg_advisory_xact_lock(7210021)')
                    if records.pg_version(connection) is None:
                        records.create_records(db, created_by=config.operator())
                db.execute('INSERT INTO machines(id, name, addresses, peer_token, updated_at) VALUES (?,?,NULL,NULL,?) ON CONFLICT DO NOTHING',
                           (machine['id'], machine['name'], records.now()))
                db.execute('INSERT INTO products(id, name, created_by, machine, created_at) VALUES (?,?,?,?,?)',
                           (product['id'], product['name'], config.operator(), machine['id'], records.now()))
                if persist:
                    persist(product)
            if persist is None:
                project.bind(product)
            result = dict(ok=True, action='new', product=product, created_tables=version is None, previous=current,
                          **notes)
            result['next'] = f'把产品 ID {product["id"]} 告诉要加入的同事，他们运行 sop-board init <项目> --join {product["id"]}'
            return result, 0
        products = listing(db, scenes) if version is not None else []
        if join:
            chosen = next((p for p in products if p['id'] == join), None)
            if chosen is None:
                return dict(ok=False, error_code='product-not-found',
                            problems=[dict(field='join', problem=f'共享库里没有 ID 为 {join} 的产品；从下面的清单里选，或用 --new 新建')],
                            products=products), 1
            product = dict(id=chosen['id'], name=chosen['name'])
            machine = store.machine
            with connection.transaction():
                db.execute('INSERT INTO machines(id, name, addresses, peer_token, updated_at) VALUES (?,?,NULL,NULL,?) ON CONFLICT DO NOTHING',
                           (machine['id'], machine['name'], records.now()))
                if persist:
                    persist(product)
            if persist is None:
                project.bind(product)
            return dict(ok=True, action='join', product=product, previous=current, **notes), 0
        return dict(ok=False, error_code='product-choice-required', problems=[dict(field='product', problem=(
            '还没有选择产品：问使用者这个项目要加入下面哪个已有产品，还是新建一个；得到答复后运行 '
            f'sop-board init {project.name} --join <ID> 或 sop-board init {project.name} --new [--product <名称>]。'
            '不要自行判断或绑定'))], products=products, bound=current, local=dict(scenarios=len(scenes)),
            shared_database=dict(host=shared['host'], port=shared['port'], dbname=shared['dbname'])), 1
    finally:
        connection.close()
