"""声明范围内的前置数据准备、脱敏记录与场景结果关联。"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import yaml

from .credentials import ENV_NAME, names, redact, response_bytes
from .errors import APIError
from .kernel import S, session_id
from .service import canonical, now


RECIPE_NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')


def text(value):
    return isinstance(value, str) and bool(value.strip()) and '\0' not in value


def texts(value, *, empty=False):
    return isinstance(value, list) and (empty or bool(value)) and all(text(v) for v in value)


def validate_config(value, problem):
    if not isinstance(value, dict):
        problem('fixtures', '须为含 environment、scope 的对象')
        return None
    for field in value.keys() - {'environment', 'scope', 'credential_env', 'adhoc'}:
        problem('fixtures.' + field, '格式外字段')
    if not text(value.get('environment')):
        problem('fixtures.environment', '须为非空文字')
    if not texts(value.get('scope')):
        problem('fixtures.scope', '须为非空文字数组')
    credentials = value.get('credential_env', [])
    if (not isinstance(credentials, list)
            or not all(isinstance(v, str) and ENV_NAME.fullmatch(v) for v in credentials)
            or len(set(credentials)) != len(credentials)):
        problem('fixtures.credential_env', '须为不重复的环境变量名数组')
    if type(value.get('adhoc', False)) is not bool:
        problem('fixtures.adhoc', '须为布尔值')
    return dict(value, credential_env=credentials, adhoc=value.get('adhoc', False))


def load_recipes(data, problem):
    from .workspace import ConfigLoader
    path = data / 'fixtures' / 'recipes.yaml'
    empty = dict(recipes=[], real_only=[])
    if not path.exists():
        return empty
    def issue(field, message):
        problem('fixtures.recipes' + field, message)
    try:
        raw = path.read_text(encoding='utf-8')
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise ValueError('不接受锚点、别名或显式标签')
        value = yaml.load(raw, Loader=ConfigLoader)
        if not isinstance(value, dict):
            raise ValueError('须为含 recipes、real_only 的对象')
        for field in value.keys() - {'recipes', 'real_only'}:
            issue('.' + field, '格式外字段')
        real_only = value.get('real_only', [])
        if not texts(real_only, empty=True):
            issue('.real_only', '须为文字数组')
        recipes = value.get('recipes')
        if not isinstance(recipes, list):
            raise ValueError('recipes 须为数组')
        seen = set()
        for index, recipe in enumerate(recipes):
            prefix = f'[{index}]'
            if not isinstance(recipe, dict):
                issue(prefix, '配方须为对象')
                continue
            for field in recipe.keys() - {'name', 'title', 'params', 'creates', 'run', 'check'}:
                issue(prefix + '.' + field, '格式外字段')
            name = recipe.get('name')
            if not isinstance(name, str) or not RECIPE_NAME.fullmatch(name):
                issue(prefix + '.name', '须只含小写字母、数字与连字符，且以字母或数字开头')
            elif name in seen:
                issue(prefix + '.name', '配方名称不能重复')
            else:
                seen.add(name)
            if not text(recipe.get('title')):
                issue(prefix + '.title', '须为非空文字')
            if not texts(recipe.get('creates'), empty=True):
                issue(prefix + '.creates', '须为文字数组')
            params = recipe.get('params')
            if (not isinstance(params, dict) or not all(ENV_NAME.fullmatch(k) and text(v) for k, v in params.items())
                    or len({k.upper() for k in params}) != len(params)):
                issue(prefix + '.params', '须为参数名到非空说明的对象；参数名须能转为互不重复的环境变量名')
            for field in ('run', 'check'):
                if field == 'run' or field in recipe:
                    if not texts(recipe.get(field)):
                        issue(prefix + '.' + field, '须为非空命令参数数组，不经过 shell')
        return dict(recipes=recipes, real_only=real_only)
    except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError) as error:
        issue('', 'recipes.yaml 无法读取或格式不合格：' + str(error))
        return empty


def definition(workspace):
    if workspace.fixtures is None:
        return None
    return dict(workspace.fixtures, **workspace.fixture_recipes)


def secrets_for(workspace, declaration=None):
    declared = set(workspace.credential_names) | set(names({'fixtures': declaration or {}}))
    return [os.environ[name] for name in declared if os.environ.get(name)]


def safe(value, secrets):
    return json.loads(redact(canonical(value).encode(), secrets))


def summary(record):
    return {key: record[key] for key in ('id', 'kind', 'name', 'title', 'reason', 'params', 'batch', 'scenario',
            'started_at', 'finished_at', 'exit_code', 'status', 'resolution') if key in record}


def materialize(service, directory, batches, declaration):
    if declaration is None:
        return
    secrets = secrets_for(service.workspace, declaration)
    frozen = safe(declaration, secrets)
    (directory / 'fixtures-state.json').write_text(canonical(frozen), encoding='utf-8')
    with service.store.connection() as db:
        runs = [summary(json.loads(row['record_json'])) for row in db.execute(
            'SELECT f.record_json FROM fixture_runs f JOIN batches b ON b.id=f.batch '
            'WHERE f.workspace=? AND f.task IS NULL ORDER BY f.id', (service.workspace.name,))
            if json.loads(row['record_json'])['batch'] in batches]
    payload = {key: frozen[key] for key in ('environment', 'scope', 'adhoc', 'real_only')}
    payload['recipes'] = [{key: recipe[key] for key in ('name', 'title', 'params', 'creates')} for recipe in frozen['recipes']]
    payload['batch_runs'] = runs
    (directory / 'fixtures.json').write_text(canonical(safe(payload, secrets)), encoding='utf-8')


def link_results(db, task, batch, number, directory):
    """仅关联本任务、同批次同场景且在本次准备目录内的记录；不参与结果判定。"""
    db.execute('INSERT OR IGNORE INTO fixture_run_results(run,result_id) '
        'SELECT f.id,r.id FROM fixture_runs f JOIN results r ON r.scenario=f.scenario '
        'JOIN rounds n ON n.id=r.round_id WHERE f.task=? AND f.batch=? AND n.batch=f.batch '
        'AND n.number=? AND f.directory=?', (task, batch['id'], number, str(directory)))


def result_runs(db, result_id, workspace, *, local):
    records = [json.loads(row[0]) for row in db.execute(
        'SELECT f.record_json FROM fixture_runs f JOIN fixture_run_results l ON l.run=f.id '
        'WHERE l.result_id=? ORDER BY f.id', (result_id,))]
    if not records:
        return []
    if not local:
        # 内网只投影业务说明、参数与时间；不公开命令、输出、目录及任务会话。
        records = [{key: r[key] for key in ('kind', 'name', 'title', 'reason', 'params', 'started_at', 'finished_at')} for r in records]
    return json.loads(response_bytes(canonical(records).encode(), 'application/json', workspace.credential_names,
        local=local, local_roots=[workspace.data, *workspace.repos.values()]))


class Fixtures:
    def __init__(self, service, tasks=None):
        self.service, self.workspace, self.store, self.tasks = service, service.workspace, service.store, tasks

    def configured(self):
        self.workspace.reload()
        if not self.workspace.configured:
            raise ValueError('工作区配置不合格：' + '；'.join(p['field'] + '：' + p['problem'] for p in self.workspace.problems))

    def read(self):
        self.workspace.reload()
        with self.store.connection() as db:
            rows = [json.loads(row[0]) for row in db.execute(
                'SELECT record_json FROM fixture_runs WHERE workspace=? ORDER BY id DESC', (self.workspace.name,))]
        declaration = definition(self.workspace)
        if any(p['field'].startswith('fixtures') for p in self.workspace.problems):
            declaration = None
        return safe(dict(ok=True, configured=declaration is not None, declaration=declaration,
            problems=self.workspace.problems, runs=rows[:50],
            pending=[r for r in rows if r['kind'] == 'adhoc' and not r.get('resolution')]), secrets_for(self.workspace))

    def target(self, task_id, batch_name, owner, scenario):
        from .session import identity
        if task_id is None:
            if not batch_name:
                raise ValueError('请指定 --task 或 --batch')
            owner = identity(owner)
            with self.store.connection() as db:
                batch = dict(self.service.batch(db, self.workspace.name, batch_name))
            return batch, owner, self.workspace.data / 'fixtures' / 'runs', definition(self.workspace), scenario
        with self.store.connection() as db:
            row = db.execute('SELECT t.*,s.session_id AS owner FROM tasks t LEFT JOIN session_tasks s ON s.task=t.id '
                             'WHERE t.id=? AND t.workspace=?', (task_id, self.workspace.name)).fetchone()
        if row is None:
            raise ValueError('任务不存在或不属于本工作区')
        task = dict(row)
        if not (task['kind'] == 'execute' and task['status'] in (S.RUNNING, S.SESSION)
                or task['kind'] == 'fix' and task['status'] == S.VERIFYING):
            raise ValueError('任务须为可写结果的执行或复验任务')
        directory = self.tasks.directory(task_id)
        if task['kind'] == 'fix':
            candidates = list((directory / 'validations' / str(task['attempt'])).glob('*/retest.json'))
            if not candidates:
                raise ValueError('复验任务尚未准备')
            directory = max(candidates, key=lambda p: int(p.parent.name)).parent
        if not (directory / 'prompt.md').is_file():
            raise ValueError('任务尚未准备')
        owner = task['owner'] or session_id(directory, '')
        if not owner:
            raise ValueError('任务尚无执行会话标识')
        scope = json.loads(task['scope_json'])
        if (directory / 'retest.json').is_file():
            scope = json.loads((directory / 'retest.json').read_text(encoding='utf-8'))
        if scenario and '#' in scenario:
            batch_name, scenario = scenario.split('#', 1)
        matches = {(s.get('batch') or task['batch'], s['scenario']) for s in scope
                   if (not scenario or s['scenario'] == scenario) and (not batch_name or s.get('batch', task['batch']) == batch_name)}
        if not matches or len({b for b, _ in matches}) != 1:
            raise ValueError('场景不在任务范围内或涉及多个批次；请用 --scenario 批次#场景 明确指定')
        batch_name = next(iter(matches))[0]
        with self.store.connection() as db:
            batch = dict(self.service.batch(db, self.workspace.name, batch_name))
        state = directory / 'fixtures-state.json'
        declaration = json.loads(state.read_text(encoding='utf-8')) if state.is_file() else None
        return batch, owner, directory / 'evidence' / 'fixtures', declaration, scenario

    def run(self, *, recipe_name=None, argv=None, task=None, batch=None, owner=None, scenario=None, params=(), reason=''):
        self.configured()
        batch, owner, directory, declaration, scenario = self.target(task, batch, owner, scenario)
        if declaration is None:
            raise ValueError('本轮未声明 fixtures；请声明后重新准备任务')
        parameters = {}
        for value in params:
            key, separator, content = value.partition('=')
            if not separator or key in parameters or '\0' in content:
                raise ValueError('--param 须为不重复的 名=值')
            parameters[key] = content
        check = None
        recipe = None
        if recipe_name is not None:
            recipe = next((r for r in declaration['recipes'] if r['name'] == recipe_name), None)
            if recipe is None:
                raise ValueError('配方不存在：' + recipe_name)
            missing = recipe['params'].keys() - parameters.keys()
            if missing:
                raise ValueError('缺少参数：' + '、'.join(sorted(missing)))
            extra = parameters.keys() - recipe['params'].keys()
            if extra:
                raise ValueError('未声明的参数：' + '、'.join(sorted(extra)))
            if set(recipe['creates']) & set(declaration['real_only']):
                raise ValueError('配方产生的对象属于 real_only，须经真实入口产生')
            def expand(command):
                # 只替换已声明占位，不解析 shell、JSON 或脚本里的其他花括号。
                return [re.sub(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', lambda m: parameters.get(m[1], m[0]), arg) for arg in command]
            argv = expand(recipe['run'])
            check = expand(recipe['check']) if 'check' in recipe else None
        else:
            if not declaration['adhoc']:
                raise ValueError('工作区不允许临时写入（adhoc 为 false）')
            if not text(reason):
                raise ValueError('临时写入须用 --reason 说明原因')
            if not texts(argv):
                raise ValueError('请在 -- 后提供非空命令参数数组')
        missing = [name for name in declaration['credential_env'] if not os.environ.get(name)]
        if missing:
            raise ValueError('缺少凭据环境变量：' + '、'.join(missing))
        secrets = secrets_for(self.workspace, declaration)
        environment = dict(os.environ)
        environment.update({'FIXTURE_' + k.upper(): v for k, v in parameters.items()})
        for alias, root in self.workspace.repos.items():
            environment['SOP_REPO_' + alias.upper().replace('-', '_')] = str(root)
        record = dict(kind='recipe' if recipe else 'adhoc', name=recipe_name or 'adhoc',
            title=recipe['title'] if recipe else '临时写入', params=parameters, command=argv, reason=reason,
            workspace=self.workspace.name, batch=batch['name'], task=task, session=owner, scenario=scenario,
            environment=declaration['environment'], scope=declaration['scope'],
            credential_env=sorted(set(self.workspace.credential_names) | set(declaration['credential_env'])),
            started_at=now(), finished_at=None, exit_code=None, status='running', resolution=None)
        # 所有前置检查均已完成；首次写记录早于命令启动，中断时仍保留运行事实。
        (self.workspace.data / 'fixtures').mkdir(exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        with self.store.transaction() as db:
            run_id = db.execute('INSERT INTO fixture_runs(workspace,batch,task,scenario,directory,record_json) VALUES (?,?,?,?,?,?)',
                (self.workspace.name, batch['id'], task, scenario, str(directory.parent.parent if task else directory), canonical(safe(record, secrets)))).lastrowid
        record['id'] = run_id
        label = f'{run_id}-{recipe_name or "adhoc"}'
        path = directory / (label + '.json')
        record['path'] = str(path)
        def save():
            cleaned = safe(record, secrets)
            path.write_text(canonical(cleaned) + '\n', encoding='utf-8')
            with self.store.transaction() as db:
                db.execute('UPDATE fixture_runs SET record_json=? WHERE id=?', (canonical(cleaned), run_id))
            return cleaned
        save()
        def execute(command, phase):
            try:
                outcome = subprocess.run(command, cwd=self.workspace.data / 'fixtures', env=environment,
                    stdin=subprocess.DEVNULL, capture_output=True)
                code, stdout, stderr = outcome.returncode, outcome.stdout, outcome.stderr
            except OSError as error:
                code, stdout, stderr = 127, b'', str(error).encode()
            stdout, stderr = redact(stdout, secrets), redact(stderr, secrets)
            for stream, content in (('stdout', stdout), ('stderr', stderr)):
                (directory / f'{label}-{phase}-{stream}.txt').write_bytes(content)
            value = dict(command=command, exit_code=code, stdout=stdout.decode('utf-8', errors='replace'),
                         stderr=stderr.decode('utf-8', errors='replace'))
            record[phase] = value
            save()
            return code
        try:
            if check:
                execute(check, 'before')
            record['exit_code'] = execute(argv, 'run')
            if check:
                execute(check, 'after')
            # 核对命令失败也如实报告，不把无法核对的数据准备记为成功。
            record['exit_code'] = record['exit_code'] or next((record[p]['exit_code'] for p in ('before', 'after')
                if p in record and record[p]['exit_code']), 0)
        except KeyboardInterrupt:
            record['exit_code'] = 130
        finally:
            record.update(finished_at=now(), status='pass' if record['exit_code'] == 0 else 'fail')
            cleaned = save()
        return dict(ok=record['exit_code'] == 0, run=cleaned)

    def mark(self, run_id, *, promoted=None, dismiss=False, by=None):
        if not text(by):
            raise ValueError('请用 --by 填写标记人姓名')
        if bool(promoted) == bool(dismiss) or promoted and (not isinstance(promoted, str) or not RECIPE_NAME.fullmatch(promoted)):
            raise ValueError('请选择 --promoted <配方名称> 或 --dismiss')
        with self.store.transaction() as db:
            from .atlas import person
            by = person(by, db, self.workspace.name)
            row = db.execute('SELECT record_json FROM fixture_runs WHERE id=? AND workspace=?', (run_id, self.workspace.name)).fetchone()
            if row is None:
                raise ValueError('准备记录不存在')
            record = json.loads(row[0])
            if record['kind'] != 'adhoc':
                raise ValueError('只能标记临时写入的记录')
            record['resolution'] = dict(status='promoted' if promoted else 'dismissed', recipe=promoted, by=by.strip(), at=now())
            record = safe(record, secrets_for(self.workspace, record))
            path = Path(record['path'])
            if path.is_file():
                path.write_text(canonical(record) + '\n', encoding='utf-8')
            db.execute('UPDATE fixture_runs SET record_json=? WHERE id=?', (canonical(record), run_id))
        return dict(ok=True, run=record)


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board fixtures', description='在工作区声明范围内准备前置数据并留痕')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('list', 'run', 'exec', 'mark'):
        child = sub.add_parser(action)
        child.add_argument('targets', nargs='*', help='[工作区]' + (' <配方>' if action == 'run' else ''))
        child.add_argument('--data', type=Path)
        if action in ('run', 'exec'):
            target = child.add_mutually_exclusive_group(required=True)
            target.add_argument('--task', type=int)
            target.add_argument('--batch')
            child.add_argument('--session')
            child.add_argument('--scenario')
            if action == 'run':
                child.add_argument('--param', action='append', default=[])
            else:
                child.add_argument('--reason', required=True)
        if action == 'mark':
            child.add_argument('--run', type=int, required=True)
            target = child.add_mutually_exclusive_group(required=True)
            target.add_argument('--promoted')
            target.add_argument('--dismiss', action='store_true')
            child.add_argument('--by', help='标记人姓名')
    argv = None
    if arguments and arguments[0] == 'exec' and '--' in arguments:
        index = arguments.index('--')
        arguments, argv = arguments[:index], arguments[index + 1:]
    args = parser.parse_args(arguments)
    app = None
    try:
        targets = list(args.targets)
        recipe = targets.pop() if args.action == 'run' and targets else None
        if args.action == 'run' and not recipe:
            raise ValueError('请指定配方名称')
        if len(targets) > 1:
            raise ValueError('工作区参数过多')
        from .session import Session
        app = Session(targets[0] if targets else None, args.data)
        fixtures = Fixtures(app.service, app.tasks)
        with app.tasks.lock:
            if args.action == 'list':
                fixtures.configured()
                result = fixtures.read()
                if result['declaration']:
                    result['declaration']['recipes'] = [{k: r[k] for k in ('name', 'title', 'params', 'creates')} for r in result['declaration']['recipes']]
                result = {k: v for k, v in result.items() if k not in ('runs', 'pending')}
                result['message'] = ('允许临时写入' if result['declaration']['adhoc'] else '不允许临时写入') if result['declaration'] else '未声明前置数据'
            elif args.action == 'mark':
                result = fixtures.mark(args.run, promoted=args.promoted, dismiss=args.dismiss, by=app.person(args.by))
            else:
                if args.batch and not args.session:
                    raise ValueError('--batch 准备须提供 --session 标识')
                if args.task is not None and args.session:
                    raise ValueError('--task 使用任务的执行会话，无需 --session')
                result = fixtures.run(recipe_name=recipe, argv=argv, task=args.task, batch=args.batch, owner=args.session,
                    scenario=args.scenario, params=getattr(args, 'param', []), reason=getattr(args, 'reason', ''))
        print(json.dumps(safe(result, secrets_for(app.workspace)), ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 1
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as error:
        result = dict(ok=False, problems=[dict(field='fixtures', problem=str(error))])
    finally:
        if app:
            app.close()
    if app:
        result = safe(result, secrets_for(app.workspace))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
