"""Web 执行资产：冻结副本、逐项保真、运行绑定和经审核收入。资产正文只保存在本机。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import base64
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from uuid import uuid4

from .service import canonical, fingerprint, now


STATES = {'verified', 'reviewed-failing', 'unverified'}
CLASSES = ('same', 'review', 'violation', 'no_baseline')
LABELS = dict(same='相同', review='待审核', violation='违规', no_baseline='无基线')
REQUIRED = {'batch', 'scenario', 'scenario_fingerprint', 'case_fingerprints', 'spec', 'spec_sha256',
            'deps', 'version', 'baseline', 'state', 'origin', 'provenance', 'review', 'run', 'support'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(content)
    os.replace(temporary, path)


def component(value):
    if (not isinstance(value, str) or not value.strip() or value in ('.', '..')
            or any(char in value for char in ('/', '\\', '\0', '\n', '\r'))):
        raise ValueError('批次、场景与版本须为不含路径分隔符的单行标识：' + str(value))
    return value


def inside(root, relative):
    root = Path(root).resolve()
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or '..' in Path(relative).parts or '\0' in relative):
        raise ValueError('资产路径须为目录内的相对路径')
    path = root / relative
    cursor = root
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('资产路径不能包含符号链接：' + relative)
    if not path.resolve().is_relative_to(root):
        raise ValueError('资产路径越界')
    return path


def asset_root(entry):
    return Path('pages') / component(entry['batch']) / component(entry['scenario']) / component(str(entry['version']))


def index(root):
    file = root / 'INDEX.json'
    value = read(file) if file.is_file() else dict(version=1, entries=[])
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('entries'), list):
        raise ValueError('INDEX.json 须为 version=1、entries 数组')
    seen = set()
    for entry in value['entries']:
        if not isinstance(entry, dict) or REQUIRED - entry.keys():
            raise ValueError('库条目缺少约定字段')
        key = component(entry['batch']), component(entry['scenario'])
        if key in seen or entry['state'] not in STATES or entry['origin'] not in ('promote', 'import'):
            raise ValueError('库条目重复或状态、来源不合格')
        seen.add(key)
        inside(root, entry['spec'])
        inside(root, entry['baseline'])
        if not isinstance(entry['deps'], dict) or not isinstance(entry['case_fingerprints'], dict):
            raise ValueError('deps 与 case_fingerprints 须为对象')
        for relative in entry['deps']:
            inside(root, relative)
    return value


def version_digest(root):
    files = {}
    if root.exists():
        for file in sorted(root.rglob('*')):
            if file.name in ('.spec-library.lock',) or file.name.endswith('.tmp'):
                continue
            if file.is_symlink():
                raise ValueError('库不能包含符号链接')
            if file.is_file():
                files[file.relative_to(root).as_posix()] = sha(file.read_bytes())
    return sha(canonical(files).encode())


def ast(mode, spec, root, *, entry=None, extra=False, node_root=None):
    script = Path(__file__).parent / 'skills/playwright-acceptance/scripts/spec-ast.mjs'
    args = ['node', str(script), mode, str(spec), str(root)]
    if entry:
        args += ['library', str(root / asset_root(entry) if (root / asset_root(entry)).is_dir() else root)]
    if extra:
        args += ['runtime']
    environment = dict(os.environ)
    if node_root and not environment.get('PW_NODE_ROOT'):
        environment['PW_NODE_ROOT'] = str(node_root)
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, env=environment)
    if result.returncode:
        raise ValueError('资产静态分析失败：' + result.stderr.strip())
    return json.loads(result.stdout)


def as_bytes(value):
    return value if isinstance(value, bytes) else value.encode('utf-8')


def preview(value):
    data = as_bytes(value)
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return '<二进制 sha256=' + sha(data) + '>\n'


def portable_files(spec, directory):
    result = ast('portable', spec, directory)
    files = dict(result['files'])
    files.update({name: base64.b64decode(value) for name, value in result.get('binary', {}).items()})
    origins = result.get('origins', {})
    for name, prefix in (('library', ''), ('pages', 'pages/'), ('support', 'support/'), ('data', 'data/')):
        asset_base = directory / 'scenarios' / name
        if not asset_base.is_dir():
            continue
        for file in sorted(asset_base.rglob('*')):
            if file.is_file():
                relative = file.relative_to(asset_base).as_posix()
                inside(asset_base, relative)
                logical = (prefix + relative).removesuffix('.off')
                source_path = file.relative_to(directory).as_posix()
                if logical in files and origins.get(logical) == source_path:
                    continue  # 同一文件的导入路径已由静态分析器重定位。
                value = file.read_bytes()
                if logical in files and as_bytes(files[logical]) != value:
                    raise ValueError('资产路径冲突：' + logical)
                files[logical] = value
                origins[logical] = source_path
    return files


def freeze_candidate(directory, registered):
    """收入只使用运行摘要对应的不可变副本，后续原稿编辑不会混入本版。"""
    writable([directory])
    frozen = directory / 'spec-candidates' / uuid4().hex
    frozen.mkdir(parents=True)
    for name, expected in registered['assets']['candidate']['files'].items():
        value = inside(directory, name).read_bytes()
        if sha(value) != expected:
            raise ValueError('当前候选或执行依赖与本场景最近一次已登记运行摘要不同：' + name)
        file = inside(frozen, name)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value)
    for name in ('scope.json', 'cases.json', 'spec-ledger.json'):
        source = directory / name
        if source.is_file():
            inside(directory, name)
            (frozen / name).write_bytes(source.read_bytes())
    return frozen


def cases_for(cases, scene):
    return {case['id']: sha(canonical(case).encode()) for case in cases if case['id'] in scene.get('refs', [])}


def seed(workspace, directory, batch, scenes, entries, cases, *, directories=None):
    """仅在最终范围冻结后调用。整项先核对，再复制；已有任务清单保持不变。"""
    directory = Path(directory)
    manifest = directory / 'specs-manifest.json'
    if manifest.exists():
        return read(manifest)
    result = dict(version=1, batch=batch, seeded=[], missing=[], reference=[], conflict=[], libraries={})
    plans = []
    for scene in scenes:
        sid, entry_name = scene['id'], scene['verify']
        declaration = entries[entry_name]
        if declaration.get('surface') != 'web' or declaration.get('driver') not in (None, 'playwright-acceptance'):
            continue
        row = dict(batch=batch, scenario=sid)
        root = workspace.spec_library(entry_name, directories=directories, entries=entries)
        if root is None:
            result['missing'].append(dict(row, reason='入口未声明规范库'))
            continue
        result['libraries'][entry_name] = dict(path=str(root), sha256=version_digest(root))
        found = next((e for e in index(root)['entries'] if (e['batch'], e['scenario']) == (batch, sid)), None)
        if not found:
            result['missing'].append(dict(row, reason='库中没有本批次、编号的条目'))
            continue
        if found['state'] != 'verified' or found['scenario_fingerprint'] != fingerprint(scene):
            reason = '状态不是 verified：' + found['state'] if found['state'] != 'verified' else '场景指纹不同或来源指纹未知'
            result['reference'].append(dict(row, reason=reason, library=str(root), entry=found))
            continue
        copies = {}
        try:
            source = inside(root, found['spec'])
            reached = ast('assets', source, root, entry=found, node_root=workspace.data)
            declared = {found['spec']: found['spec_sha256'], **found['deps']}
            if any(declared.get(name) != value for name, value in reached['files'].items()):
                raise ValueError('索引遗漏执行依赖或依赖摘要不一致')
            if sha(source.read_bytes()) != found['spec_sha256']:
                raise ValueError('规范脚本摘要与索引不同')
            copies['scenarios/' + component(sid) + '.spec.mjs'] = source.read_bytes()
            prefix = asset_root(found)
            for relative, expected in found['deps'].items():
                path = inside(root, relative)
                data = path.read_bytes()
                if sha(data) != expected:
                    raise ValueError('依赖摘要与索引不同：' + relative)
                logical = (Path(relative).relative_to(prefix) if Path(relative).is_relative_to(prefix) else Path(relative)).as_posix()
                support_scripts = set()
                for support in found['support']:
                    support_path = Path(support['path'])
                    logical_path = support_path.relative_to(prefix) if support_path.is_relative_to(prefix) else support_path
                    support_manifest = inside(root, (support_path / 'manifest.json').as_posix())
                    if support_manifest.is_file():
                        for item in read(support_manifest).get('files', []):
                            if item.get('role') in ('spec', 'script', 'entry'):
                                support_scripts.add((logical_path / item['path']).as_posix())
                    if logical.startswith(logical_path.as_posix() + '/') and (logical.endswith('.spec.mjs') or Path(logical).suffix in ('.py', '.sh')):
                        support_scripts.add(logical)
                suffix = '.off' if logical in support_scripts else ''
                copies['scenarios/library/' + logical + suffix] = data
            for name in ('fixtures', 'library'):
                copies['scenarios/' + name + '.mjs'] = ("export * from './library/" + name + ".mjs';\n").encode()
            if 'scenarios/library/library.mjs' not in copies or 'scenarios/library/fixtures.mjs' not in copies:
                raise ValueError('库缺少 fixtures.mjs 或 library.mjs 执行依赖')
        except (OSError, ValueError) as error:
            result['reference'].append(dict(row, reason='资产不可冻结：' + str(error), entry=found))
            continue
        note = '批次、编号、场景指纹一致且已核实'
        if cases_for(cases, scene) != found['case_fingerprints']:
            note += '；引用用例指纹不同，场景指纹仍相同'
        plans.append(dict(row, reason=note, entry=found, copies=copies, library=str(root)))
    targets = {}
    for plan in plans:
        for name, content in plan['copies'].items():
            targets.setdefault(name, []).append((plan, content))
    for plan in plans:
        conflicts = []
        for name, content in plan['copies'].items():
            path = inside(directory, name)
            if path.exists() or len({data for _, data in targets[name]}) > 1:
                conflicts.append(name)
        if conflicts:
            result['conflict'].append({k: v for k, v in plan.items() if k != 'copies'} | dict(reason='目标文件已存在或依赖路径冲突：' + '、'.join(conflicts)))
        else:
            result['seeded'].append({k: v for k, v in plan.items() if k != 'copies'})
    approved = {(p['batch'], p['scenario']) for p in result['seeded']}
    written = set()
    for plan in plans:
        if (plan['batch'], plan['scenario']) not in approved:
            continue
        for name, data in plan['copies'].items():
            if name in written:
                continue
            path = inside(directory, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(data)
            written.add(name)
    result['library_sha256'] = sha(canonical(result['libraries']).encode())
    # 补齐运行器，已有材料逐个保留。规范脚本和已有候选都走同一随包驱动。
    if scenes:
        templates = Path(__file__).parent / 'skills/playwright-acceptance/templates'
        for name in ('playwright.config.mjs', 'global-setup.mjs', 'progress-reporter.mjs', 'spec-assets.mjs'):
            target = directory / name
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write((templates / name).read_bytes())
        for name in ('fixtures.mjs', 'connection.mjs', 'scope.mjs', 'recording.mjs', 'atlas.mjs', 'identity.mjs'):
            target = directory / 'scenarios' / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write((templates / name).read_bytes())
        library_file = directory / 'scenarios/library.mjs'
        if not library_file.exists():
            library_file.write_text('// 新写脚本的本轮依赖由执行者补齐。\n', encoding='utf-8')
    write(manifest, result)
    ledger = directory / 'spec-ledger.json'
    if not ledger.exists():
        write(ledger, [])
    return result


def result_materials(directory, row, batch):
    """普通任务取根材料，复验按批次/场景唯一选择本轮冻结目录。"""
    directory = Path(directory)
    index_file = directory / 'playwright-scopes.json'
    paths = set(directory.glob('playwright/*/scope.json'))
    split = index_file.is_file() or (directory / 'playwright').is_dir()
    if split:
        if index_file.is_file():
            declarations = read(index_file)
            if not isinstance(declarations, list):
                raise ValueError('分批范围索引须为数组')
            for item in declarations:
                path = Path(item['scope'])
                if not path.is_absolute():
                    path = directory / path
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError('分批范围路径越出复验目录')
                path = inside(directory, path.relative_to(directory).as_posix())
                if not path.is_file() or read(path).get('batch') != item.get('batch'):
                    raise ValueError('分批冻结范围缺失或批次不一致')
                paths.add(path)
        matches = []
        for path in sorted(paths):
            scope = read(inside(directory, path.relative_to(directory).as_posix()))
            if scope.get('batch') == batch and row['scenario'] in scope.get('scenarios', {}):
                matches.append(path.parent)
        if len(matches) != 1:
            # 非Web结果不属于Playwright分批范围；仍沿用原驱动登记。
            retest = read(directory / 'retest.json') if (directory / 'retest.json').is_file() else []
            entries = read(directory / 'entries.json').get('entries', {}) if (directory / 'entries.json').is_file() else {}
            scenes = [r.get('scene', {}) for r in retest if (r.get('batch'), r.get('scenario')) == (batch, row['scenario'])]
            if not matches and not row.get('spec_run') and len(scenes) == 1:
                entry = entries.get(scenes[0].get('verify'), {})
                if entry and (entry.get('surface') != 'web' or entry.get('driver') not in (None, 'playwright-acceptance')):
                    return None, None, True
            raise ValueError('结果对应的分批冻结目录缺失或归属不唯一：' + str(batch) + '#' + row['scenario'])
        selected = matches[0]
        manifest_file = selected / 'specs-manifest.json'
        if not manifest_file.is_file():
            raise ValueError('分批冻结清单缺失：' + str(batch))
    else:
        selected = directory
        manifest_file = directory / 'specs-manifest.json'
    manifest = read(manifest_file) if manifest_file.is_file() else None
    if manifest and batch is not None and manifest.get('batch') != batch:
        raise ValueError('冻结清单与结果批次不一致')
    return selected, manifest, split


def run_for_result(directory, row, *, task=None, batch=None):
    """缺摘要门槛和库关联使用同一份、按批次选定的冻结清单。"""
    directory = Path(directory)
    batch = batch or row.get('batch')
    selected, manifest, split = result_materials(directory, row, batch)
    reference = row.get('spec_run')
    if selected is None:
        return None
    if not reference:
        if (row.get('status') in ('pass', 'fail') and not (row.get('note') or '').startswith('沿用：')
                and any((r.get('batch', batch), r['scenario']) == (batch, row['scenario']) for r in (manifest or {}).get('seeded', []))):
            raise ValueError('复用脚本缺少本次运行开始时的摘要：' + row['scenario'])
        return None
    candidates = {inside(directory, reference['file']), inside(selected, reference['file'])}
    if not any(p.is_file() for p in candidates):
        candidates = {inside(selected, 'spec-runs/' + component(reference['id']) + '.json')}
    files = [p for p in candidates if p.is_file()]
    if not files or len({sha(p.read_bytes()) for p in files}) != 1:
        raise ValueError('运行摘要缺失或引用冲突')
    run = read(files[0])
    sid = row['scenario']
    item = run.get('scenarios', {}).get(sid)
    if (run.get('id') != reference.get('id') or not item
            or item.get('candidate_sha256') != reference.get('candidate_sha256')
            or batch and run.get('batch') != batch
            or run.get('scope', {}).get('scenarios', {}).get(sid) != row.get('fingerprint')):
        raise ValueError('运行摘要与场景、批次或指纹不一致：' + sid)
    source_directory = Path(run.get('directory', selected))
    if not source_directory.is_absolute():
        source_directory = selected / source_directory
    source_directory = source_directory.resolve()
    if split and source_directory != selected.resolve():
        raise ValueError('运行摘要不属于结果对应的分批冻结目录')
    if task is not None and not source_directory.is_relative_to(directory.resolve()):
        raise ValueError('运行摘要的候选目录越出任务目录')
    return dict(task=task, directory=str(source_directory), run=run, candidate=item, library=manifest)


def save_result(db, result_id, row):
    if row.get('spec_assets'):
        db.execute('INSERT INTO playwright_result_assets(result_id,task,record_json) VALUES (?,?,?)',
                   (result_id, row['spec_assets'].get('task'), canonical(row['spec_assets'])))


def public_assets(value, sid):
    return dict(reuse_count=sum(r['scenario'] == sid for r in (value.get('library') or {}).get('seeded', [])))


def result_assets(db, result_id, *, local):
    row = db.execute('SELECT a.record_json,r.scenario FROM playwright_result_assets a JOIN results r ON r.id=a.result_id WHERE a.result_id=?', (result_id,)).fetchone()
    reviews = [dict(state=r['state'], by=r['reviewer']) for r in db.execute(
        'SELECT state,reviewer FROM playwright_reviews WHERE result_id=? ORDER BY rowid', (result_id,))]
    if row:
        value = json.loads(row[0])
        return dict(value if local else public_assets(value, row['scenario']), reviews=reviews)
    return None


def summary(db, *, batch=None, task=None):
    where, params = ('t.batch=?', (batch,)) if batch is not None else ('a.task=?', (task,))
    count = 0
    for row in db.execute('SELECT a.record_json,r.scenario FROM playwright_result_assets a JOIN results r ON r.id=a.result_id JOIN rounds t ON t.id=r.round_id WHERE ' + where, params):
        count += public_assets(json.loads(row[0]), row['scenario'])['reuse_count']
    reviews = [dict(state=r['state'], by=r['reviewer']) for r in db.execute(
        'SELECT v.state,v.reviewer FROM playwright_reviews v JOIN results r ON r.id=v.result_id JOIN rounds t ON t.id=r.round_id '
        'LEFT JOIN playwright_result_assets a ON a.result_id=r.id WHERE ' + where + ' ORDER BY v.rowid', params)]
    return dict(reuse_count=count, reviews=reviews)


def writable(paths):
    for path in paths:
        path = Path(path)
        parent = path
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK | (os.X_OK if parent.is_dir() else 0)):
            raise ValueError('资产目标不可写：' + str(path))
        if path.exists() and not os.access(path.parent, os.W_OK | os.X_OK):
            raise ValueError('资产目标目录不可写：' + str(path.parent))


def library_ledgers(root):
    values = {}
    for name in ('text-ledger.json', 'assertion-ledger.json'):
        rows = read(root / name) if (root / name).is_file() else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(name + ' 须为条目数组')
        values[name] = rows
    return values


@contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.spec-library.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def ledger_entries(directory, batch, sid):
    file = directory / 'spec-ledger.json'
    values = read(file) if file.is_file() else []
    if not isinstance(values, list):
        raise ValueError('spec-ledger.json 须为条目数组')
    rows = [r for r in values if isinstance(r, dict) and (r.get('batch'), r.get('scenario')) == (batch, sid)]
    if len({r.get('id') for r in rows}) != len(rows):
        raise ValueError('账本 id 重复')
    return rows


def withdrawal(db, workspace, batch, item, baseline_cases, current_cases, sid):
    change = item.get('sop_change')
    if not isinstance(change, dict) or change.keys() != {'case', 'before_fingerprint', 'after_fingerprint', 'decision_id'}:
        return False
    decision = db.execute("SELECT * FROM decisions WHERE id=? AND workspace=? AND batch=? AND source='case-amend'",
                          (change['decision_id'], workspace, batch)).fetchone()
    if not decision:
        return False
    # case-amend 的正式决定保存实际上传号；绑定该次上传前后真实的用例正文。
    match = re.search(r'\n上传 #(\d+)；用例：([^\n]+)', decision['text'])
    cid = change['case']
    paired = db.execute('SELECT case_sha256 FROM case_pairings WHERE scenario=? AND case_id=? AND version='
        '(SELECT v.id FROM versions v JOIN batches b ON b.id=v.batch WHERE b.workspace=? AND b.name=? ORDER BY v.id DESC LIMIT 1)',
        (sid, cid, workspace, batch)).fetchone()
    if (current_cases.get(cid) != change['after_fingerprint'] or not paired or paired[0] != change['after_fingerprint']):
        return False
    if not match or cid not in match[2].split('、'):
        return False
    bid = db.execute('SELECT id FROM batches WHERE workspace=? AND name=?', (workspace, batch)).fetchone()
    number = int(match[1])
    after = db.execute('SELECT body FROM case_versions WHERE batch=? AND case_id=? AND upload=?', (bid[0], cid, number)).fetchone()
    before = db.execute('SELECT body FROM case_versions WHERE batch=? AND case_id=? AND upload<? ORDER BY upload DESC LIMIT 1', (bid[0], cid, number)).fetchone()
    return bool(before and after and change['before_fingerprint'] == sha(canonical(json.loads(before[0])).encode())
        and change['after_fingerprint'] == sha(canonical(json.loads(after[0])).encode())
        and baseline_cases.get(cid) == change['before_fingerprint'])


def check_one(root, entry, candidate, directory, batch, sid, *, db=None, workspace=None):
    current = ast('analyze', candidate, directory, entry=entry if directory == root else None)['items']
    baseline = []
    if entry:
        base = inside(root, entry['baseline'])
        source = inside(base, entry['spec'])
        baseline = ast('analyze', source, base, entry=entry)['items']
    before, after = {r['assertion_id']: r for r in baseline}, {r['assertion_id']: r for r in current}
    ledger = ledger_entries(directory, batch, sid)
    cases_file = directory / 'cases.json'
    current_cases = {c['id']: sha(canonical(c).encode()) for c in read(cases_file)} if cases_file.is_file() else {}
    report = dict(batch=batch, scenario=sid, baseline=entry['version'] if entry else None,
                  same=[], review=[], violation=[], no_baseline=[])
    for aid in sorted(before.keys() | after.keys()):
        old, new = before.get(aid), after.get(aid)
        matching = [r for r in ledger if r.get('assertion_id') == aid]
        reason = '首次收入，须逐条对照业务要求审核' if not entry else '新增断言' if not old else '断言相同'
        category = 'no_baseline' if not entry else 'review' if not old else 'same'
        valid = []
        if old and (not new or old['signature'] != new['signature']):
            category, reason = ('violation', '断言或数据核对缺失，没有合法撤销或结构改写账本') if not new else ('review', '对象、计算来源、匹配器、参数、步骤或条件变化')
            for record in matching:
                if not all(isinstance(record.get(k), str) and record[k].strip() for k in ('id', 'reason', 'ref')):
                    continue
                if 'node_path' in record and new:
                    old_nodes = {v['node_path']: v['value'] for v in old['text_nodes']}
                    new_nodes = {v['node_path']: v['value'] for v in new['text_nodes']}
                    pointer = record['node_path']
                    if old_nodes.get(pointer) == record.get('old') and new_nodes.get(pointer) == record.get('new') and record.get('old') != record.get('new'):
                        valid.append(record)
                elif record.get('old') in (old['text'], old['signature']):
                    if record.get('kind') == 'rewrite' and new and record.get('new') in (new['text'], new['signature']) and record.get('sop_change') is None:
                        valid.append(record)
                    elif record.get('kind') == 'withdraw' and not new and record.get('new') is None and db is not None and withdrawal(db, workspace, batch, record, entry['case_fingerprints'], current_cases, sid):
                        valid.append(record); category, reason = 'review', '撤销已绑定 cases put 决定与用例前后指纹，仍须本次审核'
            if not new and not valid:
                # 一个旧断言可明确改写为具有新标识的断言，但不能只凭同样文字存在。
                for record in matching:
                    target = next((v for v in current if record.get('new') in (v['text'], v['signature'])), None)
                    if (record.get('kind') == 'rewrite' and record.get('old') in (old['text'], old['signature']) and target
                            and record.get('reason') and record.get('ref') and record.get('sop_change') is None):
                        valid.append(record); category, reason = 'review', '断言结构改写，须审核 old/new 去向'
        if old and new and valid and not any(r.get('kind') == 'rewrite' for r in valid):
            expected = deepcopy(old['signature'])
            for record in valid:
                target = expected
                parts = record['node_path'].strip('/').split('/')
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                last = int(parts[-1]) if isinstance(target, list) else parts[-1]
                target[last] = record['new']
            # 单节点账本不能批准同文案的其他节点或结构差异。
            if expected != new['signature']:
                valid = []
                reason += '；文字账本未覆盖全部变化'
        widening = bool(old and new and not old['signature'].get('negated') and
            any(n.get('regex') for n in new['text_nodes']) and old['signature'] != new['signature'])
        if widening:
            reason += '；正向文字可能扩大接受集合，须说明具体放宽理由'
            valid = [r for r in valid if r['reason'].strip() not in ('跟随 UI', '跟随UI', '结构调整', '同步UI', '同步 UI')]
        report[category].append(dict(assertion_id=aid, reason=reason, old=old, new=new, ledger=valid, widening=widening))
    report['ledger'] = ledger
    return report


def qualified(report):
    if report['violation']:
        raise ValueError('保真检查有违规，未收入')
    covered = {r['id'] for item in report['review'] for r in item['ledger']}
    if any(row.get('id') not in covered for row in report['ledger']):
        raise ValueError('账本存在未精确对应本次断言变化的条目')
    for item in report['review']:
        if item['old'] and not item['ledger']:
            raise ValueError('待审核项缺少完整且精确对应的账本：' + item['assertion_id'])


def candidate_identity(registered):
    """候选作者取与该候选运行绑定的执行者，产品实现者单独保存。"""
    return dict(author=registered['executor'], executor=registered['executor'], implementer=registered.get('implementer'))


class Specs:
    def __init__(self, workspace, store):
        self.workspace, self.store = workspace, store

    def targets(self, task_id, selected, library=None):
        if not task_id:
            roots = [Path(library).expanduser().resolve()] if library else list(dict.fromkeys(
                self.workspace.spec_library(name) for name in self.workspace.entries if self.workspace.entries[name].get('spec_library')))
            targets = []
            for root in roots:
                for entry in index(root)['entries']:
                    key = entry['batch'] + '#' + entry['scenario']
                    if not selected or key in selected:
                        targets.append((root, entry, inside(root, entry['spec']), root, entry['batch'], entry['scenario'], None))
            if selected and set(selected) - {x[4] + '#' + x[5] for x in targets}:
                raise ValueError('所选场景不在规范库中')
            return targets
        with self.store.connection() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=? AND workspace=?', (task_id, self.workspace.name)).fetchone()
        if not task:
            raise ValueError('任务不存在或不属于当前工作区')
        task_dir = self.workspace.data / 'tasks' / str(task_id)
        scopes = [task_dir / 'scope.json'] if (task_dir / 'scope.json').is_file() else []
        scopes += list(task_dir.glob('playwright/*/scope.json'))
        if not scopes:
            with self.store.connection() as db:
                saved = db.execute('SELECT a.record_json FROM playwright_result_assets a JOIN results r ON r.id=a.result_id '
                    'JOIN rounds t ON t.id=r.round_id JOIN batches b ON b.id=t.batch WHERE a.task=? AND b.workspace=? ORDER BY r.id DESC',
                    (task_id, self.workspace.name)).fetchall()
            known = {}
            for row in saved:
                record = json.loads(row[0])
                key = record['run']['batch'] + '#' + next(sid for sid, item in record['run']['scenarios'].items() if item == record['candidate'])
                if key not in known and (not selected or key in selected):
                    known[key] = Path(record['directory']) / 'scope.json'
            scopes = list(dict.fromkeys(known.values()))
            if not scopes:
                candidates = list(task_dir.glob('validations/*/*/playwright/*/scope.json'))
                if candidates:
                    newest = max((p.parents[2] for p in candidates), key=lambda p: (int(p.parent.name), int(p.name)))
                    scopes = [p for p in candidates if p.parents[2] == newest]
        targets = []
        for scope_file in scopes:
            scope = read(scope_file)
            directory = scope_file.parent
            source = read(directory / 'entries.json') if (directory / 'entries.json').is_file() else read(task_dir / 'entries.json')
            import yaml
            doc = yaml.safe_load((directory / 'scenarios.yaml').read_text())
            scenes = {s['id']: s for node in doc['sops'] for s in node['scenarios']}
            for sid in scope['scenarios']:
                batch = scope['batch']; key = batch + '#' + sid
                if selected and key not in selected:
                    continue
                entry_name = scenes[sid]['verify']
                root = Path(library).resolve() if library else self.workspace.spec_library(entry_name, entries=source['entries'])
                if root is None:
                    raise ValueError('入口尚未声明 spec_library')
                entry = next((e for e in index(root)['entries'] if (e['batch'], e['scenario']) == (batch, sid)), None)
                spec = directory / 'scenarios' / (component(sid) + '.spec.mjs')
                if not spec.is_file():
                    raise ValueError('任务候选不存在：' + str(spec))
                targets.append((root, entry, spec, directory, batch, sid, dict(task)))
        if selected and set(selected) != {t[4] + '#' + t[5] for t in targets}:
            raise ValueError('所选场景不在任务冻结范围或有重复的复验范围')
        return targets

    def check(self, target):
        root, entry, spec, directory, batch, sid, _ = target
        with self.store.connection() as db:
            return check_one(root, entry, spec, directory, batch, sid, db=db, workspace=self.workspace.name)

    def registered(self, task_id, batch, sid):
        with self.store.connection() as db:
            row = db.execute('SELECT a.record_json,r.*,t.number,t.product_digest,t.implementer,t.batch AS batch_id '
                'FROM playwright_result_assets a JOIN results r ON r.id=a.result_id JOIN rounds t ON t.id=r.round_id '
                'JOIN batches b ON b.id=t.batch WHERE a.task=? AND b.workspace=? AND b.name=? AND r.scenario=? AND t.kind=? '
                'ORDER BY r.id DESC LIMIT 1', (task_id, self.workspace.name, batch, sid, 'agent')).fetchone()
            if not row:
                return None
            latest = db.execute('SELECT r.id FROM results r JOIN rounds t ON t.id=r.round_id JOIN batches b ON b.id=t.batch '
                "WHERE b.workspace=? AND b.name=? AND r.scenario=? AND t.kind='agent' ORDER BY r.id DESC LIMIT 1",
                (self.workspace.name, batch, sid)).fetchone()
            if latest[0] != row['id']:
                return None
            value = dict(row); value['assets'] = json.loads(value.pop('record_json'))
            version = db.execute('SELECT v.content FROM versions v JOIN rounds r ON r.version=v.id WHERE r.id=?', (row['round_id'],)).fetchone()
            import yaml
            document = yaml.safe_load(version[0])
            scene = next(s for n in document['sops'] for s in n['scenarios'] if s['id'] == sid)
            # 当前引用用例与范围中的原文一起固定在任务材料，而不是用库或外部输入补造。
            value['scene'] = scene
            return value

    def review(self, target, task_id):
        root, entry, spec, directory, batch, sid, task = target
        check = self.check(target)
        summary = ast('assets', spec, directory, extra=True)
        registered = self.registered(task_id, batch, sid)
        old = inside(root, entry['baseline']) / entry['spec'] if entry else None
        diff = ''.join(difflib.unified_diff(preview(old.read_bytes()).splitlines(True) if old else [],
            preview(spec.read_bytes()).splitlines(True), fromfile='baseline', tofile='candidate'))
        portable = portable_files(spec, directory)
        old_files = {}
        if entry:
            prefix = asset_root(entry)
            for name in entry['deps']:
                logical = (Path(name).relative_to(prefix) if Path(name).is_relative_to(prefix) else Path(name)).as_posix()
                old_files[logical] = inside(root, entry['baseline']).joinpath(name).read_bytes()
        dependency_diff = [dict(file=name, diff=''.join(difflib.unified_diff(
                preview(old_files.get(name, b'')).splitlines(True), preview(portable.get(name, b'')).splitlines(True),
                fromfile='baseline/' + name, tofile='candidate/' + name)))
            for name in sorted(old_files.keys() | (portable.keys() - {'(spec)'})) if as_bytes(old_files.get(name, b'')) != as_bytes(portable.get(name, b''))]
        history_source = inside(root, entry['baseline']) / 'source.spec.mjs' if entry and entry['origin'] == 'import' else None
        source_diff = ''.join(difflib.unified_diff(preview(history_source.read_bytes()).splitlines(True), preview(spec.read_bytes()).splitlines(True),
            fromfile='historical-source', tofile='candidate')) if history_source and history_source.is_file() else None
        full_review = bool(entry and entry['origin'] == 'import' and entry['state'] == 'unverified')
        return dict(version=1, task=task_id, batch=batch, scenario=sid, candidate=summary, dependency_diff=dependency_diff,
            provenance=entry.get('provenance') if entry else None, historical_source_diff=source_diff,
            historical_notice='历史导入未经本包审核，须对照原始来源和业务要求复核全部断言' if full_review else None,
            baseline=entry['version'] if entry else None, diff=diff, check=check,
            candidate_identity=candidate_identity(registered) if registered else None,
            pending=check['review'] + check['no_baseline'] + (check['same'] if full_review else []), run=registered, generated_at=now(),
            note='生成材料不是审核结论；审核者须逐条核对要求、断言去向和真实运行依据')

    def promote(self, target, task_id, by, reviewer, state):
        root, entry, spec, directory, batch, sid, task = target
        if not isinstance(by, str) or not by.strip():
            raise ValueError('请填写实际审核人姓名')
        if reviewer is not None and not reviewer.strip():
            raise ValueError('审核会话标识不能为空')
        registered = self.registered(task_id, batch, sid)
        if not registered:
            raise ValueError('没有本任务、本批次、本场景已登记的运行依据')
        identity = candidate_identity(registered)
        frozen = freeze_candidate(directory, registered)
        frozen_spec = frozen / spec.relative_to(directory)
        frozen_target = root, entry, frozen_spec, frozen, batch, sid, task
        material = self.review(frozen_target, task_id)
        material['source_snapshot'] = str(frozen)
        if not material['run'] or material['run']['id'] != registered['id']:
            raise ValueError('收入准备期间运行依据变化，请重新核对')
        identities = {str(v).strip().casefold() for v in (task['session_id'], registered['executor'], registered['implementer']) if v}
        with self.store.connection() as db:
            claim = db.execute('SELECT session_id FROM session_tasks WHERE task=?', (task_id,)).fetchone()
            if claim:
                identities.add(claim[0].strip().casefold())
        if reviewer and reviewer.strip().casefold() in identities:
            raise ValueError('审核会话不能等于任务执行或实现会话')
        if material['candidate'] != registered['assets']['candidate']:
            raise ValueError('当前候选或执行依赖与本场景最近一次已登记运行摘要不同')
        # 同目录最近一次开跑也必须是登记对应的那一次；不能回退借旧运行。
        runs = [read(p) for p in (directory / 'spec-runs').glob('*.json')]
        runs = [r for r in runs if r.get('batch') == batch and sid in r.get('scenarios', {})]
        if not runs or max(runs, key=lambda r: r['started_at'])['id'] != registered['assets']['run']['id']:
            raise ValueError('本场景最近一次运行尚未登记或与登记依据不同')
        if (state == 'verified' and registered['status'] != 'pass' or
                state == 'reviewed-failing' and (registered['status'] != 'fail' or not (registered.get('finding') or '').strip())):
            raise ValueError('收入状态与已登记结果或发现说明不符')
        if registered['fingerprint'] != read(directory / 'scope.json')['scenarios'][sid]:
            raise ValueError('已登记结果与本轮冻结场景指纹不同')
        qualified(material['check'])
        portable = portable_files(frozen_spec, frozen)
        cases_file = frozen / 'cases.json'
        if not cases_file.is_file():
            cases_file = self.workspace.data / 'tasks' / str(task_id) / 'cases.json'
        cases = read(cases_file)
        review = dict(by=by.strip(), kind='session' if reviewer else 'person', session=reviewer or None,
                      at=now(), candidate_sha256=material['candidate']['candidate_sha256'])
        writable([root, directory])
        with lock(root):
            ledgers = library_ledgers(root)
            current = index(root)
            actual = next((e for e in current['entries'] if (e['batch'], e['scenario']) == (batch, sid)), None)
            if actual != entry:
                raise ValueError('审核期间规范库版本变化，请重新审核')
            version = str(int(entry['version']) + 1) if entry else '1'
            record = dict(batch=batch, scenario=sid, scenario_fingerprint=registered['fingerprint'],
                case_fingerprints=cases_for(cases, registered['scene']), spec='specs/' + batch + '/' + sid + '.spec.mjs',
                spec_sha256=sha(as_bytes(portable['(spec)'])), deps={}, version=version,
                baseline='baseline/' + batch + '/' + sid + '/' + version, state=state, origin='promote',
                provenance=dict(task=task_id, run=registered['assets']['run']['id'], candidate_snapshot=frozen.relative_to(directory).as_posix(), **identity), review=review,
                run=dict(batch=batch, round=registered['number'], result_id=registered['id'], status=registered['status'],
                    product_digest=registered['product_digest']), support=[])
            prefix = asset_root(record)
            bundle = {record['spec']: as_bytes(portable.pop('(spec)'))}
            for name, text in portable.items():
                rel = (prefix / name).as_posix()
                bundle[rel] = as_bytes(text); record['deps'][rel] = sha(as_bytes(text))
                if name.startswith('support/') and name.endswith('/manifest.json'):
                    key = Path(name).parent.name
                    record['support'].append(dict(id=key, path=str(prefix / Path(name).parent), enabled=False))
            baseline = inside(root, record['baseline'])
            if baseline.exists():
                raise ValueError('基线版本目录已存在，未覆盖')
            targets = [root / 'INDEX.json', root / 'text-ledger.json', root / 'assertion-ledger.json', baseline,
                       *(inside(root, rel) for rel in bundle)]
            if entry:
                targets.append(inside(root, '_history/' + batch + '/' + sid + '/' + str(entry['version'])))
            writable(targets)
            if entry:
                history = inside(root, '_history/' + batch + '/' + sid + '/' + str(entry['version']))
                if history.exists():
                    raise ValueError('历史版本目录已存在，未覆盖')
                for rel in [entry['spec'], *entry['deps']]:
                    dst = inside(history, rel); dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(inside(root, entry['baseline']) .joinpath(rel).read_bytes())
                write(history / 'entry.json', entry)
            for rel, data in bundle.items():
                frozen = inside(baseline, rel); frozen.parent.mkdir(parents=True, exist_ok=True); frozen.write_bytes(data)
                active = inside(root, rel); active.parent.mkdir(parents=True, exist_ok=True); active.write_bytes(data)
            write(baseline / 'entry.json', record)
            for filename, predicate in [('text-ledger.json', lambda r: 'node_path' in r), ('assertion-ledger.json', lambda r: 'node_path' not in r)]:
                existing = ledgers[filename]
                for item in material['check']['ledger']:
                    if predicate(item):
                        existing.append(dict(item, **identity,
                            review=review, candidate_sha256=review['candidate_sha256'], baseline=entry['version'] if entry else None))
                write(root / filename, existing)
            current['entries'] = [e for e in current['entries'] if (e['batch'], e['scenario']) != (batch, sid)] + [record]
            write(root / 'INDEX.json', current)
            write(directory / ('spec-review-' + sid + '-' + version + '.json'), material | dict(review=review, state=state))
        with self.store.transaction() as db:
            db.execute('INSERT INTO playwright_reviews(result_id,version,state,reviewer,review_json) VALUES (?,?,?,?,?)',
                (registered['id'], version, state, by.strip(), canonical(review)))
        return dict(ok=True, entry=record)

    def import_cw2(self, source, batch):
        source = Path(source).expanduser().resolve()
        old_root = inside(source, 'lib/specs')
        old = read(inside(old_root, 'INDEX.json'))
        if not isinstance(old, dict) or not isinstance(old.get('scenarios'), list):
            raise ValueError('首版只接受含 lib/specs/INDEX.json.scenarios 的 cw2 accept-run 目录')
        with self.store.connection() as db:
            row = db.execute('SELECT content FROM versions WHERE batch=(SELECT id FROM batches WHERE workspace=? AND name=?) ORDER BY id DESC LIMIT 1',
                             (self.workspace.name, batch)).fetchone()
        if not row:
            raise ValueError('目标批次尚无已登记场景')
        import yaml
        scenes = {s['id']: s for n in yaml.safe_load(row[0])['sops'] for s in n['scenarios']}
        roots = {self.workspace.spec_library(s['verify']) for s in scenes.values() if self.workspace.entries[s['verify']].get('spec_library')}
        if len(roots) != 1:
            raise ValueError('导入批次须唯一绑定一个 Web 规范库')
        root = roots.pop()
        planned, skipped = [], []
        old_ledgers = library_ledgers(old_root)
        ids = [row.get('scenario') for row in old['scenarios']]
        if len(set(ids)) != len(ids):
            raise ValueError('来源索引场景编号重复，未导入')
        templates = Path(__file__).parent / 'skills/playwright-acceptance/templates'
        for original in old['scenarios']:
            sid = component(original['scenario'])
            canonical_name = original.get('canonical')
            if not canonical_name:
                skipped.append(dict(scenario=sid, reason='来源没有规范脚本')); continue
            script = inside(old_root, canonical_name).read_bytes()
            snapshot = inside(old_root, original['snapshot']) if original.get('snapshot') else None
            manifest = read(snapshot / 'manifest.json') if snapshot and (snapshot / 'manifest.json').is_file() else {}
            source_name = manifest.get('source', {}).get('spec')
            source_bytes = inside(snapshot, source_name).read_bytes() if snapshot and source_name else None
            if source_bytes is not None and manifest.get('source', {}).get('snapshot_sha256') not in (None, sha(source_bytes)):
                raise ValueError('来源快照摘要与 manifest 不一致：' + sid)
            changed = source_bytes is None or script != source_bytes
            history_results = [row for row in [original.get('result'), *original.get('versions', [])] if isinstance(row, dict)]
            def recorded_at(row):
                try:
                    stamp = datetime.fromisoformat(row['executed_at'].replace('Z', '+00:00'))
                    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    return None
            explicit = original.get('latest_result')
            uncertain = False
            latest_reason = ''
            if explicit is not None:
                latest = explicit if isinstance(explicit, dict) else {}
                stamp = recorded_at(latest)
                if stamp is None:
                    uncertain, latest_reason = True, '显式最近结果缺少有效执行时间，不能回退采用更早通过'
                elif any(recorded_at(row) is not None and recorded_at(row) > stamp for row in history_results):
                    uncertain, latest_reason = True, '显式最近结果与来源历史时间顺序冲突'
            else:
                dated = [row for row in history_results if recorded_at(row) is not None]
                latest = max(dated, key=lambda row: (recorded_at(row), row.get('status') != 'pass')) if dated else {}
                if len(dated) != len(history_results) or not dated:
                    uncertain, latest_reason = True, '来源结果时间缺失或非法，无法确定最近结果'
            source_task = original.get('source_task')
            source_result = original.get('result') or {}
            registered = None
            # 旧 run 如 r01 是任务内浏览器运行号。按实际任务登记收据与执行时间关联该批次结果，
            # 不把 r01 猜成看板轮次1，也不借其他批次或当前场景补指纹。
            with self.store.connection() as db:
                candidates = db.execute('SELECT r.*,t.number,t.product_digest,t.version,t.idempotency_key '
                    'FROM results r JOIN rounds t ON t.id=r.round_id JOIN batches b ON b.id=t.batch '
                    "WHERE b.workspace=? AND b.name=? AND r.scenario=? AND t.kind='agent' ORDER BY r.id DESC",
                    (self.workspace.name, batch, sid)).fetchall()
                for item in candidates:
                    keys = (f'execute-{source_task}-', f'fix-{source_task}-', f'session-fix-{source_task}-')
                    receipt = db.execute('SELECT result_json FROM task_receipts WHERE task=?', (source_task,)).fetchall() if type(source_task) is int else []
                    rounds = [n for receipt_ in receipt for n in json.loads(receipt_[0]).get('rounds', []) if type(n) is int]
                    matches_task = any((item['idempotency_key'] or '').startswith(key) for key in keys) or item['number'] in rounds
                    matches_time = not source_result.get('executed_at') or item['executed_at'] == source_result['executed_at']
                    if matches_task and matches_time and item['status'] == source_result.get('status'):
                        registered = dict(item); break
                case_prints = {}
                if registered:
                    union = db.execute('SELECT case_id,case_sha256 FROM case_pairings WHERE version=? AND scenario=?', (registered['version'], sid)).fetchall()
                    case_prints = {c['case_id']: c['case_sha256'] for c in union}
            verified = latest.get('status') == 'pass' and not changed and not uncertain
            entry = dict(batch=batch, scenario=sid, scenario_fingerprint=registered['fingerprint'] if registered else None,
                case_fingerprints=case_prints, spec='specs/' + batch + '/' + sid + '.spec.mjs', spec_sha256=sha(script),
                deps={}, version='1', baseline='baseline/' + batch + '/' + sid + '/1',
                state='verified' if verified else 'unverified', origin='import',
                provenance=dict(source_task=source_task, run=original.get('run'), source_sha256=sha(source_bytes) if source_bytes is not None else None,
                    canonical_sha256=sha(script), rewritten=changed, source_result=latest, imported_at=now(), latest_result_uncertain=uncertain, latest_result_reason=latest_reason,
                    note='历史导入，未经本包审核' + ('；规范已改写，未在新版界面真实运行' if changed else '') + ('；' + latest_reason if uncertain else ''),
                    fingerprint_source='看板来源登记结果' if registered else '未知，只列参考'),
                review=None, run=dict(batch=batch, round=registered['number'], result_id=registered['id'],
                    status=registered['status'], product_digest=registered['product_digest']) if registered else None, support=[])
            assets = {}
            for file in sorted((source / 'lib/pages').rglob('*')):
                if file.is_file():
                    relative = 'pages/' + file.relative_to(source / 'lib/pages').as_posix()
                    assets[relative] = inside(source / 'lib', relative).read_bytes()
            # 产品辅助模块属于导入资产；本包不加载或执行它们，也不收编外层调度。
            for file in (source / 'lib').glob('*.mjs'):
                assets[file.name] = inside(source / 'lib', file.name).read_bytes()
            for file in templates.glob('*.mjs'):
                if file.name not in ('global-setup.mjs', 'playwright.config.mjs', 'progress-reporter.mjs', 'example.spec.mjs'):
                    assets.setdefault(file.name, file.read_bytes())
            old_fixture = inside(source, 'lib/kit/scenarios/fixtures.mjs')
            if old_fixture.is_file():
                assets['fixtures.mjs'] = old_fixture.read_bytes()
            # 明文相对导出取代旧 kit 中可变的共享绝对路径加载。
            if 'library.mjs' not in assets:
                exports = "export * from './index.mjs';\n" if 'index.mjs' in assets else ''.join(
                    "export * as " + re.sub(r'\W', '_', Path(name).stem) + " from './" + name + "';\n"
                    for name in sorted(assets) if name.startswith('pages/') and name.count('/') == 1 and name.endswith('.mjs'))
                assets['library.mjs'] = exports.encode()
            for support in original.get('support', []):
                key = support if isinstance(support, str) else support['id']
                support_dir = inside(old_root, 'support/' + component(key))
                if not support_dir.is_dir():
                    continue
                logical = 'support/' + key
                for file in support_dir.rglob('*'):
                    if file.is_file():
                        rel = file.relative_to(old_root).as_posix()
                        assets[rel] = inside(old_root, rel).read_bytes()
                entry['support'].append(dict(id=key, path=str(asset_root(entry) / logical), enabled=False))
            for name, data in assets.items():
                entry['deps'][(asset_root(entry) / name).as_posix()] = sha(data)
            planned.append((entry, script, assets, manifest, source_bytes))
        writable([root])
        with lock(root):
            target_ledgers = library_ledgers(root)
            current = index(root)
            existing = {(e['batch'], e['scenario']) for e in current['entries']}
            if any((e['batch'], e['scenario']) in existing for e, *_ in planned):
                raise ValueError('导入会覆盖已有当前版本，未导入；请在独立库核对或经正常审核收入')
            prospective = [root / 'INDEX.json', root / 'text-ledger.json', root / 'assertion-ledger.json']
            for entry, _, assets, _, _ in planned:
                prospective += [root / entry['spec'], root / entry['baseline'], *(root / asset_root(entry) / name for name in assets)]
                if (root / entry['baseline']).exists() or (root / entry['spec']).exists():
                    raise ValueError('目标已存在，未覆盖导入资产')
            writable(prospective)
            for entry, script, assets, manifest, source_bytes in planned:
                bundle = {entry['spec']: script} | {(asset_root(entry) / name).as_posix(): data for name, data in assets.items()}
                for name, data in bundle.items():
                    for base in (root, root / entry['baseline']):
                        file = inside(base, name); file.parent.mkdir(parents=True, exist_ok=True)
                        with file.open('xb') as stream:
                            stream.write(data)
                write(root / entry['baseline'] / 'entry.json', entry)
                write(root / entry['baseline'] / 'source-manifest.json', manifest)
                if source_bytes is not None:
                    (root / entry['baseline'] / 'source.spec.mjs').write_bytes(source_bytes)
                current['entries'].append(entry)
            for filename in ('text-ledger.json', 'assertion-ledger.json'):
                values = old_ledgers[filename]
                merged = target_ledgers[filename]
                merged += [dict(r, batch=batch, scenario=r.get('spec'), origin='import',
                    review=None, note='历史导入，未经本包审核') for r in values]
                write(root / filename, merged)
            write(root / 'INDEX.json', current)
        return dict(ok=True, imported=[e for e, *_ in planned], skipped=skipped)


class ReadStore:
    def __init__(self, data):
        self.data = data

    @contextmanager
    def connection(self):
        db = sqlite3.connect((self.data / 'workspace.sqlite3').as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def show_checks(reports, format_):
    result = dict(ok=not any(r['violation'] for r in reports), reports=reports)
    if format_ == 'json':
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(report['batch'] + '#' + report['scenario'] + '：' + '，'.join(LABELS[k] + ' ' + str(len(report[k])) for k in CLASSES))
            for key in CLASSES:
                for item in report[key]:
                    print('  [' + LABELS[key] + '] ' + item['assertion_id'] + '：' + item['reason'])
    return 0 if result['ok'] else 1


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board playwright specs')
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('check', 'review', 'promote', 'import'):
        sub = actions.add_parser(name)
        sub.add_argument('--data', type=Path)
        if name != 'import':
            sub.add_argument('--task', type=int, required=name != 'check')
            sub.add_argument('--scenario', action='extend', nargs='+', default=[], required=name != 'check')
        if name == 'check':
            sub.add_argument('--library', type=Path)
            sub.add_argument('--format', choices=('text', 'json'), default='text')
        if name == 'review':
            sub.add_argument('--out', type=Path, required=True)
        if name == 'promote':
            sub.add_argument('--reviewed-by', required=True)
            sub.add_argument('--reviewer-session')
            sub.add_argument('--state', choices=('verified', 'reviewed-failing'), default='verified')
        if name == 'import':
            sub.add_argument('--from', dest='source', type=Path, required=True)
            sub.add_argument('--batch', required=True)
    args = parser.parse_args(arguments)
    from .workspace import Workspace
    from .storage import Store
    try:
        if args.action == 'check' and args.library and not args.task and args.data is None:
            root = args.library.expanduser().resolve()
            entries = [e for e in index(root)['entries'] if not args.scenario or e['batch'] + '#' + e['scenario'] in args.scenario]
            if args.scenario and set(args.scenario) != {e['batch'] + '#' + e['scenario'] for e in entries}:
                raise ValueError('所选场景不在规范库中')
            return show_checks([check_one(root, e, inside(root, e['spec']), root, e['batch'], e['scenario']) for e in entries], args.format)
        workspace = Workspace(data=args.data, readonly=args.action in ('check', 'review'))
        if not workspace.configured:
            raise ValueError('工作区配置不合格：' + '；'.join(p['problem'] for p in workspace.problems))
        os.environ.setdefault('PW_NODE_ROOT', str(workspace.data))
        specs = Specs(workspace, ReadStore(workspace.data) if args.action in ('check', 'review') else Store(workspace.data))
        if args.action == 'import':
            result = specs.import_cw2(args.source, component(args.batch))
        else:
            targets = specs.targets(args.task, args.scenario, getattr(args, 'library', None))
            if args.action in ('review', 'promote') and len(targets) != 1:
                raise ValueError('review/promote 每次只接受一个批次#场景')
            if args.action == 'check':
                reports = [specs.check(target) for target in targets]
                return show_checks(reports, args.format)
            elif args.action == 'review':
                result = specs.review(targets[0], args.task)
                if any(args.out.resolve().is_relative_to(repo) for repo in workspace.repos.values()):
                    raise ValueError('审核过程材料须放在产品仓库外')
                write(args.out, result)
            else:
                result = specs.promote(targets[0], args.task, args.reviewed_by, args.reviewer_session, args.state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok', True) else 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('规范脚本操作失败：' + str(error), file=sys.stderr)
        return 1
