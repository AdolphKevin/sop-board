"""守护：产品仓库 <场景目录>/regression.yaml 登记场景到低成本测试的关联。

守护有效（缺一不可）：场景存在且指纹相同；测试文件是仓库内的普通文件且摘要一致；coverage 逐条按顺序覆盖
全部 expected 与 forbidden_effects 且文字与当前一致；检出能力证据存在；acceptance 是同指纹的独立真实入口通过；
没有登记过漏检。只覆盖部分预期的守护不写进索引。
"""
import hashlib
from pathlib import Path
import re

import yaml

from . import gitops
from .config import strict_yaml
from .scenarios import GUARD_INDEX, split_key

LAYERS = ('unit', 'api', 'component', 'browser')
COMMAND_ID = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def index_path_for(project, checkout):
    directory = checkout.scenarios_dir(project)
    return directory / GUARD_INDEX if directory else None


def index_path(context):
    return index_path_for(context.project, context.checkout)


def _relative(value):
    return (isinstance(value, str) and value.strip() and '\0' not in value and not Path(value).is_absolute()
            and '..' not in Path(value).parts)


def validate_index(value):
    """结构校验；返回问题列表。"""
    problems = []

    def problem(field, message):
        problems.append(dict(field=field, problem=message))
    if not isinstance(value, dict) or value.get('version') != 1:
        return [dict(field='version', problem='须为 1')]
    for extra in value.keys() - {'version', 'commands', 'guards'}:
        problem(extra, '格式外字段')
    commands = value.get('commands', {})
    if not isinstance(commands, dict):
        problem('commands', '须为命令编号到命令的对象')
        commands = {}
    for name, command in commands.items():
        field = f'commands.{name}'
        if not COMMAND_ID.fullmatch(str(name)) or not isinstance(command, dict):
            problem(field, '命令编号须为小写编号，声明须为对象')
            continue
        for extra in command.keys() - {'repo', 'cwd', 'argv', 'timeout', 'browser', 'env'}:
            problem(f'{field}.{extra}', '格式外字段')
        if not isinstance(command.get('repo'), str):
            problem(field + '.repo', '须为仓库别名')
        if 'cwd' in command and not (_relative(command['cwd']) or command['cwd'] == '.'):
            problem(field + '.cwd', '须为仓内相对目录')
        argv = command.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and '\0' not in x for x in argv):
            problem(field + '.argv', '须为非空参数数组，不经过 shell')
        if type(command.get('timeout', 900)) is not int or not 1 <= command.get('timeout', 900) <= 7200:
            problem(field + '.timeout', '须为 1～7200 秒')
        if 'browser' in command and not isinstance(command['browser'], str):
            problem(field + '.browser', '须为身份编号')
        if 'env' in command and (not isinstance(command['env'], dict) or not all(
                re.fullmatch('[A-Z_][A-Z0-9_]*', k) and not k.startswith('SOP_') and isinstance(v, str)
                for k, v in command['env'].items())):
            problem(field + '.env', '须为变量名到文字的对象，不能使用 SOP_ 开头的变量')
    guards = value.get('guards', {})
    if not isinstance(guards, dict):
        problem('guards', '须为场景键到守护的对象')
        guards = {}
    for name, guard in guards.items():
        field = f'guards.{name}'
        try:
            split_key(name)
        except ValueError as error:
            problem(field, str(error))
            continue
        if not isinstance(guard, dict):
            problem(field, '须为对象')
            continue
        for extra in guard.keys() - {'fingerprint', 'acceptance', 'tests', 'coverage', 'detection', 'ledger'}:
            problem(f'{field}.{extra}', '格式外字段')
        if not isinstance(guard.get('fingerprint'), str) or not SHA256.fullmatch(guard['fingerprint']):
            problem(field + '.fingerprint', '须为场景指纹')
        acceptance = guard.get('acceptance')
        if not isinstance(acceptance, dict) or type(acceptance.get('result')) is not int:
            problem(field + '.acceptance', '须含独立通过的结果编号 result')
        tests = guard.get('tests')
        ids = set()
        if not isinstance(tests, list) or not tests:
            problem(field + '.tests', '须为非空测试数组')
            tests = []
        for index, test in enumerate(tests):
            where = f'{field}.tests[{index}]'
            if not isinstance(test, dict):
                problem(where, '须为对象')
                continue
            for extra in test.keys() - {'id', 'repo', 'path', 'sha256', 'name', 'layer', 'command'}:
                problem(f'{where}.{extra}', '格式外字段')
            if not isinstance(test.get('id'), str) or not COMMAND_ID.fullmatch(test['id']) or test['id'] in ids:
                problem(where + '.id', '须为不重复的小写编号')
            ids.add(test.get('id'))
            if not _relative(test.get('path')):
                problem(where + '.path', '须为仓内相对路径')
            if not isinstance(test.get('sha256'), str) or not SHA256.fullmatch(test['sha256']):
                problem(where + '.sha256', '须为测试文件的 sha256')
            if not isinstance(test.get('name'), str) or not test['name'].strip():
                problem(where + '.name', '须为测试名')
            if test.get('layer') not in LAYERS:
                problem(where + '.layer', '应为 ' + '、'.join(LAYERS))
            if test.get('command') not in commands:
                problem(where + '.command', '须为已声明的命令编号')
            if not isinstance(test.get('repo'), str):
                problem(where + '.repo', '须为仓库别名')
        coverage = guard.get('coverage')
        if not isinstance(coverage, dict) or coverage.keys() != {'expected', 'forbidden_effects'}:
            problem(field + '.coverage', '须含 expected 与 forbidden_effects')
        else:
            for part in ('expected', 'forbidden_effects'):
                items = coverage[part]
                if not isinstance(items, list) or not all(
                        isinstance(item, dict) and type(item.get('index')) is int and isinstance(item.get('text'), str)
                        and isinstance(item.get('tests'), list) and item['tests'] and set(item['tests']) <= ids
                        for item in items):
                    problem(f'{field}.coverage.{part}', '每项须含 index、text 与非空的已声明测试编号')
        detection = guard.get('detection')
        if (not isinstance(detection, dict) or detection.get('method') not in ('mutation', 'known-defect')
                or not isinstance(detection.get('assertion'), str) or not isinstance(detection.get('evidence'), str)
                or not SHA256.fullmatch(detection['evidence'])):
            problem(field + '.detection', '须含 method（mutation 或 known-defect）、assertion 与证据 sha256')
        if not isinstance(guard.get('ledger', []), list):
            problem(field + '.ledger', '须为数组')
    return problems


def load_index(path):
    """读取守护索引；不存在时返回空索引。结构不合格抛出 ValueError。"""
    if path is None or not path.exists():
        return dict(version=1, commands={}, guards={})
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{GUARD_INDEX} 须为普通文件')
    try:
        value = strict_yaml(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ValueError(f'{GUARD_INDEX} 无法读取：{error}') from error
    problems = validate_index(value)
    if problems:
        raise ValueError(f'{GUARD_INDEX} 不合格：' + '；'.join(f"{p['field']} {p['problem']}" for p in problems[:5]))
    value.setdefault('commands', {})
    value.setdefault('guards', {})
    return value


def tests_digest(guard):
    return gitops.digest(sorted((t['repo'], t['path'], t['sha256'], t['name']) for t in guard['tests']))


def test_file(checkout, test):
    root = checkout.paths.get(test['repo'])
    if root is None:
        raise ValueError(f"测试仓库 {test['repo']} 不在检出中")
    path = root / test['path']
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"测试文件不存在或越界：{test['repo']}:{test['path']}")
    return path


def check(context, db, scene_key, scene, guard, commands):
    """单个守护是否有效；返回 (True, '') 或 (False, 原因)。"""
    if guard['fingerprint'] != scene['fingerprint']:
        return False, '场景内容已变化，守护过期'
    for test in guard['tests']:
        command = commands.get(test['command'])
        if command is None or command['repo'] not in context.checkout.paths:
            return False, f"命令 {test['command']} 不可用"
        try:
            path = test_file(context.checkout, test)
        except ValueError as error:
            return False, str(error)
        if hashlib.sha256(path.read_bytes()).hexdigest() != test['sha256']:
            return False, f"测试文件已改动：{test['repo']}:{test['path']}"
    for part in ('expected', 'forbidden_effects'):
        items = guard['coverage'][part]
        if [(item['index'], item['text']) for item in items] != list(enumerate(scene[part])):
            return False, f'coverage 没有逐条覆盖当前的 {part}'
    if not context.store.evidence_path(guard['detection']['evidence']).is_file():
        return False, '检出能力证据不存在'
    row = db.execute('SELECT * FROM results WHERE id=?', (guard['acceptance']['result'],)).fetchone()
    file, scenario = split_key(scene_key)
    if (row is None or row['kind'] != 'agent' or row['status'] != 'pass' or row['independence'] != 'independent'
            or row['fingerprint'] != scene['fingerprint'] or row['file'] != file or row['scenario'] != scenario):
        return False, 'acceptance 不是同指纹的独立真实入口通过'
    if db.execute('SELECT 1 FROM guard_misses WHERE file=? AND scenario=? AND fingerprint=? AND tests_digest=?',
                  (file, scenario, scene['fingerprint'], tests_digest(guard))).fetchone():
        return False, '曾经漏检，须重新沉淀'
    return True, ''


def validity(context, scenes):
    """场景键 → (有效, 原因)。索引不合格时全部无效并给出原因。"""
    try:
        index = load_index(index_path(context))
    except ValueError as error:
        return {key: (False, str(error)) for key in scenes}
    result = {}
    with context.store.connection() as db:
        for key in scenes:
            guard = index['guards'].get(key)
            result[key] = (False, '没有守护') if guard is None else check(context, db, key, scenes[key], guard, index['commands'])
    return result


def test_exclusions(context):
    return test_exclusions_for(context.project, context.checkout)


def test_exclusions_for(project, checkout):
    """守护测试文件不计入产品快照：沉淀测试不会让已有通过变成待复测。"""
    try:
        index = load_index(index_path_for(project, checkout))
    except ValueError:
        return {}
    result = {}
    for guard in index['guards'].values():
        for test in guard['tests']:
            result.setdefault(test['repo'], []).append(test['path'])
    return result
