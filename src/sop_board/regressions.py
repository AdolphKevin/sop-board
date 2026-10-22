"""回归报告的整份核对、关联登记与按访问范围读取；不写场景执行结果。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from .repositories import git
from .scenario_file import load_scenarios
from .workspace import repository


RUN_STATUS = {'passed': '通过', 'failed': '失败', 'not-run': '未运行'}
CI_STATUS = {'not-integrated': '未接入', 'unverified': '已接入，未验证', 'ran': '已运行'}
LAYERS = {'unit': '单元', 'api-db': '接口与数据库', 'component': '组件', 'browser': '浏览器端到端'}


def invalid(field, reason):
    raise ValueError(field + '：' + reason)


def object_fields(value, field, required, optional=()):
    if not isinstance(value, dict):
        invalid(field, '应为对象')
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        invalid(field, '；'.join(filter(None, [
            '缺少字段 ' + '、'.join(sorted(missing)) if missing else '',
            '格式外字段 ' + '、'.join(sorted(extra)) if extra else ''])))
    return value


def text(value, field):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        invalid(field, '应为非空文字且不含空字节')
    return value


def array(value, field, nonempty=False):
    if not isinstance(value, list) or nonempty and not value:
        invalid(field, '应为非空数组' if nonempty else '应为数组')
    return value


def choice(value, field, values):
    if not isinstance(value, str) or value not in values:
        invalid(field, '应为 ' + '、'.join(values))


def sha256(value, field):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        invalid(field, '应为文件内容的 SHA-256 摘要')


def commit_file(root, commit, path):
    records = git(root, 'ls-tree', '-z', commit, '--', path).split(b'\0')
    for record in records:
        if not record:
            continue
        info, name = record.split(b'\t', 1)
        mode, kind, oid = info.split()
        if name.decode('utf-8') == path and kind == b'blob' and mode in (b'100644', b'100755'):
            return git(root, 'cat-file', 'blob', oid.decode())
    raise ValueError('提交中没有该普通测试文件：' + path)


def validate_test(workspace, test, field, label):
    object_fields(test, field, ('id', 'repo', 'identity', 'root', 'path', 'sha256', 'name', 'layer', 'command', 'commit'), ('tag',))
    for key in ('id', 'repo', 'identity', 'root', 'path', 'name', 'command'):
        text(test[key], field + '.' + key)
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', test['id']):
        invalid(field + '.id', '只使用字母、数字、点、下划线或连字符')
    if 'tag' in test:
        text(test['tag'], field + '.tag')
    if label not in test['name'] and label not in test.get('tag', ''):
        invalid(field, '测试名或 tag 须包含 ' + label)
    choice(test['layer'], field + '.layer', LAYERS)
    sha256(test['sha256'], field + '.sha256')
    if workspace.identities.get(test['repo']) != test['identity']:
        invalid(field + '.identity', '与工作区登记的仓库身份不一致')
    root = Path(test['root'])
    if not root.is_absolute():
        invalid(field + '.root', '须为实际仓库根目录的绝对路径')
    try:
        root = root.resolve()
    except RuntimeError:
        invalid(field + '.root', '仓库路径存在循环链接，无法解析')
    if repository(root, workspace.data) != test['identity']:
        invalid(field + '.identity', '与实际仓库 origin 身份不一致')
    relative = PurePosixPath(test['path'])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or '\\' in test['path'] or relative.as_posix() != test['path']:
        invalid(field + '.path', '须为不含 .. 的仓内相对文件路径')
    commit = test['commit']
    if commit is not None:
        if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', commit):
            invalid(field + '.commit', '须为完整提交号，未提交填 null')
        if git(root, 'rev-parse', '--verify', commit + '^{commit}').decode().strip() != commit:
            invalid(field + '.commit', '不是实际提交号')
        raw = commit_file(root, commit, test['path'])
    else:
        path = root / test['path']
        if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)) or not path.resolve().is_relative_to(root) or not path.is_file():
            invalid(field + '.path', '测试须为仓库内的普通文件，不能经过符号链接')
        raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != test['sha256']:
        invalid(field + '.sha256', '与实际测试文件内容摘要不一致')
    # 提交后原报告可以再次登记；用具体 HEAD 提交的文件核对后才升级状态。
    if commit is None:
        try:
            head = git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()
            if hashlib.sha256(commit_file(root, head, test['path'])).hexdigest() == test['sha256']:
                commit = head
        except (ValueError, subprocess.SubprocessError):
            pass
    return dict(test, root=str(root), commit=commit)


def execution_list(value, field, tests):
    result = {}
    declared = {test['id']: test['name'] for test in tests}
    for index, item in enumerate(array(value, field)):
        prefix = f'{field}[{index}]'
        object_fields(item, prefix, ('name', 'status'), ('test',))
        name = text(item['name'], prefix + '.name')
        choice(item['status'], prefix + '.status', ('pass', 'fail', 'skip'))
        identifier = item.get('test')
        if 'test' in item:
            if not isinstance(identifier, str) or declared.get(identifier) != name:
                invalid(prefix + '.test', '须引用名称一致的声明测试 id')
        else:
            matches = [key for key, value in declared.items() if value == name]
            if len(matches) > 1:
                invalid(prefix, '同名测试须用 test 字段注明对应的测试 id')
            identifier = matches[0] if matches else None
        if identifier is not None:
            if identifier in result:
                invalid(prefix, '实际执行清单含重复的声明测试')
            result[identifier] = item['status']
    return result


def validate_runs(scene, field, tests):
    identifiers = {test['id'] for test in tests}
    run = object_fields(scene['local_run'], field + '.local_run', ('status', 'output', 'executed_tests', 'reason'))
    choice(run['status'], field + '.local_run.status', RUN_STATUS)
    text(run['reason'], field + '.local_run.reason')
    if not isinstance(run['output'], str):
        invalid(field + '.local_run.output', '应为原始输出位置，未运行可为空')
    executed = execution_list(run['executed_tests'], field + '.local_run.executed_tests', tests)
    complete = bool(tests) and all(executed.get(identifier) in ('pass', 'fail') for identifier in identifiers)
    expected = 'not-run' if not complete else 'failed' if any(executed[identifier] == 'fail' for identifier in identifiers) else 'passed'
    if run['status'] != 'not-run' and (run['status'] != expected or not run['output'].strip()):
        invalid(field + '.local_run', '声明测试未全部实际执行、结果不一致或缺原始输出，不能声明该运行结果')
    ci = object_fields(scene['ci'], field + '.ci', ('status', 'job', 'command', 'run', 'commit', 'executed_tests', 'output'))
    choice(ci['status'], field + '.ci.status', CI_STATUS)
    for key in ('job', 'command', 'run', 'commit', 'output'):
        if not isinstance(ci[key], str):
            invalid(field + '.ci.' + key, '应为文字')
    executed = execution_list(ci['executed_tests'], field + '.ci.executed_tests', tests)
    if ci['status'] != 'not-integrated':
        for key in ('job', 'command'):
            text(ci[key], field + '.ci.' + key)
    if ci['status'] == 'ran':
        for key in ('run', 'commit', 'output'):
            text(ci[key], field + '.ci.' + key)
        if not tests or not all(executed.get(identifier) in ('pass', 'fail') for identifier in identifiers):
            invalid(field + '.ci', '缺少声明测试的实际执行结果，CI 只能记为已接入，未验证')


def validate_scene(service, db, scene, field):
    from .service import fingerprint
    workspace = service.workspace
    object_fields(scene, field, ('batch', 'scenario_file', 'scenario', 'fingerprint', 'confirmed_by',
        'independent_acceptance', 'pass_evidence', 'coverage', 'tests', 'local_run', 'detection', 'ci'), ('findings',))
    for key in ('batch', 'scenario_file', 'scenario'):
        text(scene[key], field + '.' + key)
    sha256(scene['fingerprint'], field + '.fingerprint')
    batch = service.batch(db, workspace.name, scene['batch'])
    path = workspace.scenario_path(scene['batch'])
    if scene['scenario_file'] != workspace.display_path(path):
        invalid(field + '.scenario_file', '与工作区当前场景文件路径不一致')
    document = load_scenarios(path.read_text(encoding='utf-8'), entries=set(workspace.entries))
    if document['metadata']['batch'] != scene['batch']:
        invalid(field + '.batch', '与场景文件批次不一致')
    original = next((s for n in document['sops'] for s in n['scenarios'] if s['id'] == scene['scenario']), None)
    stored = db.execute('SELECT fingerprint FROM scenarios WHERE batch=? AND scenario=? AND current=1',
                        (batch['id'], scene['scenario'])).fetchone()
    if original is None or stored is None:
        invalid(field + '.scenario', '当前场景不存在')
    if scene['fingerprint'] != fingerprint(original) or stored['fingerprint'] != scene['fingerprint']:
        invalid(field + '.fingerprint', '与当前场景内容指纹不一致，请先同步或重新沉淀')
    for person in array(scene['confirmed_by'], field + '.confirmed_by', True):
        text(person, field + '.confirmed_by')
    choice(scene['independent_acceptance'], field + '.independent_acceptance', ('completed', 'incomplete'))
    evidence = object_fields(scene['pass_evidence'], field + '.pass_evidence',
        ('kind', 'record', 'scenario_fingerprint', 'product_commit', 'product_digest', 'executor'))
    choice(evidence['kind'], field + '.pass_evidence.kind', ('self-check', 'acceptance'))
    for key in ('record', 'product_commit', 'executor'):
        text(evidence[key], field + '.pass_evidence.' + key)
    sha256(evidence['product_digest'], field + '.pass_evidence.product_digest')
    if evidence['scenario_fingerprint'] != scene['fingerprint']:
        invalid(field + '.pass_evidence', '通过证据与当前场景内容指纹不一致')
    tests, ids, targets = [], set(), set()
    for index, test in enumerate(array(scene['tests'], field + '.tests')):
        test = validate_test(workspace, test, f'{field}.tests[{index}]', scene['batch'] + '#' + scene['scenario'])
        target = (test['identity'], test['path'], test['name'])
        if test['id'] in ids or target in targets:
            invalid(field + '.tests', '测试 id 须唯一，同一仓库与文件中的测试不能重复声明')
        ids.add(test['id'])
        targets.add(target)
        tests.append(test)
    coverage = object_fields(scene['coverage'], field + '.coverage', ('expected', 'forbidden_effects'))
    for kind in ('expected', 'forbidden_effects'):
        items = array(coverage[kind], field + '.coverage.' + kind)
        if len(items) != len(original[kind]):
            invalid(field + '.coverage.' + kind, '须按原文逐项列全')
        for index, (item, requirement) in enumerate(zip(items, original[kind])):
            prefix = f'{field}.coverage.{kind}[{index}]'
            object_fields(item, prefix, ('index', 'text', 'tests', 'remaining'))
            if type(item['index']) is not int or item['index'] != index or item['text'] != requirement:
                invalid(prefix, '编号或原文与当前场景不一致')
            refs = array(item['tests'], prefix + '.tests')
            if any(not isinstance(ref, str) or ref not in ids for ref in refs) or len(set(refs)) != len(refs):
                invalid(prefix + '.tests', '须引用本场景内不重复的测试 id')
            if not isinstance(item['remaining'], str) or not refs and not item['remaining'].strip():
                invalid(prefix + '.remaining', '未覆盖时须说明仍需真实入口验收的部分')
    validate_runs(scene, field, tests)
    detection = object_fields(scene['detection'], field + '.detection', ('status', 'method', 'record', 'assertion', 'reason'))
    choice(detection['status'], field + '.detection.status', ('verified', 'not-run'))
    choice(detection['method'], field + '.detection.method', ('known-defect', 'mutation', 'none'))
    for key in ('record', 'assertion', 'reason'):
        if not isinstance(detection[key], str):
            invalid(field + '.detection.' + key, '应为文字')
    if detection['status'] == 'verified':
        if not tests:
            invalid(field + '.detection', '没有声明测试，检出能力须记为未运行')
        if detection['method'] == 'none':
            invalid(field + '.detection', '已验证须注明已知缺陷或受控改错')
        for key in ('record', 'assertion'):
            text(detection[key], field + '.detection.' + key)
    else:
        text(detection['reason'], field + '.detection.reason')
    for finding in array(scene.get('findings', []), field + '.findings'):
        text(finding, field + '.findings')
    return batch['id'], tests


def submit(service, report):
    from .service import canonical, now
    object_fields(report, 'report', ('schema_version', 'registered_by', 'session', 'scenarios'))
    if type(report['schema_version']) is not int or report['schema_version'] != 1:
        invalid('schema_version', '仅接受报告格式 1')
    for key in ('registered_by', 'session'):
        text(report[key], key)
    with service.workspace_lock, service.store.transaction() as db:
        prepared, seen = [], set()
        for index, scene in enumerate(array(report['scenarios'], 'scenarios', True)):
            batch, tests = validate_scene(service, db, scene, f'scenarios[{index}]')
            key = (batch, scene['scenario'])
            if key in seen:
                invalid('scenarios', '同一批次的场景重复')
            seen.add(key)
            prepared.append((batch, scene, tests))
        registered_at, links = now(), []
        for batch, scene, tests in prepared:
            for test in tests:
                values = dict(batch=batch, scenario=scene['scenario'], fingerprint=scene['fingerprint'],
                    scenario_file=scene['scenario_file'], session=report['session'], repo=test['repo'],
                    identity=test['identity'], root=test['root'], path=test['path'], sha256=test['sha256'],
                    test_id=test['id'], test_name=test['name'], layer=test['layer'], command=test['command'],
                    commit_id=test['commit'], status='已提交' if test['commit'] else '待提交',
                    coverage_json=canonical(scene['coverage']), pass_evidence_json=canonical(scene['pass_evidence']),
                    local_run_json=canonical(scene['local_run']), ci_json=canonical(scene['ci']),
                    details_json=canonical({k: scene[k] for k in ('confirmed_by', 'independent_acceptance', 'detection', 'findings') if k in scene} | {'tag': test.get('tag', '')}),
                    registered_at=registered_at, registered_by=report['registered_by'])
                # 历史指纹保留；同一测试重新登记及待提交→已提交更新同一条记录。
                keys = ','.join(values)
                updates = ','.join(f'{key}=excluded.{key}' for key in values if key not in ('batch', 'scenario', 'fingerprint', 'identity', 'path', 'test_name'))
                db.execute(f'INSERT INTO regression_links({keys}) VALUES ({",".join("?" for _ in values)}) '
                    f'ON CONFLICT(batch,scenario,fingerprint,identity,path,test_name) DO UPDATE SET {updates}', tuple(values.values()))
                links.append(dict(batch=scene['batch'], scenario=scene['scenario'], test=test['id'], status=values['status']))
        return dict(ok=True, links=links, registered_at=registered_at)


def read_links(db, batch, scenario=None, *, local=False):
    links = []
    for stored in db.execute('SELECT r.*,s.fingerprint AS current_fingerprint FROM regression_links r '
        'LEFT JOIN scenarios s ON s.batch=r.batch AND s.scenario=r.scenario AND s.current=1 '
        'WHERE r.batch=? AND (? IS NULL OR r.scenario=?) ORDER BY r.id', (batch['id'], scenario, scenario)):
        row = dict(stored)
        details = json.loads(row['details_json'])
        run, ci = json.loads(row['local_run_json']), json.loads(row['ci_json'])
        state = row['status'] if row['fingerprint'] == row['current_fingerprint'] or row['status'] == '已放弃' else '已过期，需重新沉淀'
        link = {key: row[key] for key in ('id', 'scenario', 'fingerprint', 'test_id', 'repo', 'identity', 'layer', 'commit_id', 'sha256', 'registered_at', 'registered_by')}
        link.update(status=state, coverage=json.loads(row['coverage_json']),
                    independent_acceptance=details['independent_acceptance'],
                    local_run=dict(status=run['status']), ci=dict(status=ci['status'], results={
                        status: sum(item['status'] == status for item in ci['executed_tests']) for status in ('pass', 'fail', 'skip')}))
        if local:
            link.update({key: row[key] for key in ('scenario_file', 'session', 'root', 'path', 'test_name', 'command')})
            link.update(local_run=run, ci=ci, pass_evidence=json.loads(row['pass_evidence_json']), **details)
        links.append(link)
    return links


def ci_label(ci):
    label = CI_STATUS[ci['status']]
    if ci['status'] == 'ran':
        counts = ci.get('results') or {status: sum(item['status'] == status for item in ci['executed_tests']) for status in ('pass', 'fail')}
        label += f"（通过 {counts['pass']}，失败 {counts['fail']}）"
    return label


def coverage_lines(link):
    covered, remaining = [], []
    for key, label in (('expected', '预期'), ('forbidden_effects', '禁止副作用')):
        for item in link['coverage'][key]:
            title = f"{label} {item['index'] + 1}：{item['text']}"
            if link['test_id'] in item['tests']:
                covered.append(title)
            if item['remaining']:
                remaining.append(title + '；' + item['remaining'])
    return covered, remaining
