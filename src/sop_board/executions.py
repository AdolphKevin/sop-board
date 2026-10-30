"""固定工作区快照，在已就绪的适配环境中执行并登记结果。"""

import hashlib
from contextlib import nullcontext
from importlib.resources import files
import json
from pathlib import Path
from string import Template
import sqlite3
import subprocess

import yaml

from .kernel import (InputChanged, EXECUTION_QUEUE, OUTPUT_ACTIVE, S, codex_failure, session_id, sql_states,
                     terminate_group, wait_codex)

from .errors import APIError, reject
from .repositories import changed_files, git
from .service import canonical, fingerprint, now
from .statistics import calculate, pending_scope
from .merge_state import read_holds, held_scenarios
from .tasks import judgment_object, invalid_judgment_constant
from .validation import validate_round
from .playwright import driver_skill, write_scope
from .credentials import append_blocked, blocked_results, names as credential_names


REASONS = dict(new='新增', changed='内容变化', failed='失败重跑', rerun='重跑')
REASONS['source-changed'] = '产品变化待判定'


def write_brief(directory, scope, entries, atlas_info, fixtures_declared, *, task_id, identities=False):
    """执行速查卡（D86）：开场只读它和提示词、技能核心；细则按需读取。不超过 5KB。"""
    counts = {}
    for item in scope:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    entry_lines = []
    for name, entry in sorted(entries.items()):
        extra = ''
        if entry.get('surface') == 'web':
            policy = dict(video=True, trace=True) | entry.get('web_evidence', {})
            extra = f"，录屏 {'开' if policy['video'] else '关'}、trace {'开' if policy['trace'] else '关'}"
            if name in atlas_info.get('versions', {}):
                extra += f"，交互地图版本 {atlas_info['versions'][name]}"
        entry_lines.append(f"- {name}（{entry.get('surface')}）：证据 {'、'.join(entry.get('evidence', []))}{extra}")
    lines = [f'# 执行速查卡（任务 #{task_id}）', '',
             '先读本卡，再按下面顺序取材料；参考文档只在遇到对应情况时再读。', '',
             '## 本轮范围', '',
             f"- 待执行 {len(scope)} 个场景：" + '，'.join(f"{REASONS.get(k, k)} {v}" for k, v in sorted(counts.items())) + '（明细见 pending.json）。',
             *entry_lines[:8], *([f'- 另有 {len(entry_lines) - 8} 个入口，见 entries.json。'] if len(entry_lines) > 8 else []), '',
             '## 阅读顺序', '',
             '1. scenarios.yaml：场景原文，预期只来自这里；cases.json 是细节预期的来源用例。',
             '2. pending.json；有 source-changed 时再读 changes.json、previous-results.json，按提示词判定影响。']
    if atlas_info:
        lines.append('3. atlas.json、bindings.json：页面、控件、各语言文案与定位器，每个场景步骤对应的页面和控件，以及界面对照决定。')
    if fixtures_declared:
        lines.append(('4' if atlas_info else '3') + '. fixtures.json：前置数据的范围与配方。')
    if atlas_info:
        unbound = atlas_info.get('unbound') or []
        lines += ['', '## 用地图操作', '',
                  '- 用模板 atlas.mjs 的 openPage、locate、act 按地图操作，找不到时才只读探测或查阅前端源码。',
                  '- 地图缺失或有误时，在任务目录 atlas-feedback.jsonl 写一行：{"page", "kind": "missing|locator|name|reach|other", "detail", "scenario"}，不改地图。',
                  '- 地图不提供预期；atlas.json 中 stale 列出的页面已过期，以实际页面为准。',
                  '- 界面对照（bindings.json 的 checks）：“按 SOP”与“未定”的条目，实际文案不符即判失败，finding 写场景要求与实际文案。']
        if unbound:
            lines.append('- 未绑定的场景：' + '、'.join(unbound[:20]) + ('…' if len(unbound) > 20 else '') + '，按地图索引查找页面。')
    if identities:
        lines += ['', '## 身份', '',
                  '- identities.json 列出各身份的浏览器、验证码方式、上限与当前计数。用 identity.mjs 的 as(身份) 取该身份已登录的页面（设置 PW_IDENTITIES），不退出，也不在同一浏览器里换账号。',
                  f'- 真实登录或请求验证码前运行 sop-board identity record --entry <入口> --identity <身份> --event login|code --task {task_id}；被拒时把该身份的场景记受阻。',
                  '- 需要人做的事（人机验证、OAuth、邮箱验证等）用 sop-board attention add 登记，写明影响的场景。']
    lines += ['', '## 前置数据', '']
    if fixtures_declared:
        lines += [f'- 无关前置数据先用 sop-board fixtures run <配方> --task {task_id} --scenario <场景> [--param 名=值]；'
                  '没有合适配方且 fixtures.json 中 adhoc 为 true 时，才用 fixtures exec --reason <说明> 在声明范围内临时写入。',
                  '- real_only 对象与被验收流程应产生的对象不得写库代替；无法准备时记受阻，finding 写缺什么配方、范围或真实流程。']
    else:
        lines.append('- 本工作区未声明前置数据配方：前置不满足时记受阻，finding 写明缺什么，不自行写库。')
    lines += ['', '## 时间上限与停止', '',
              '- 模板缺省上限：单场景 4 分钟，定位或操作 15 秒，导航 30 秒；可用 PW_SCENARIO_TIMEOUT_MS、PW_ACTION_TIMEOUT_MS、PW_NAVIGATION_TIMEOUT_MS 覆盖。',
              '- 只重跑没通过的场景；业务失败最多再确认一次。单个场景累计 15 分钟或 4 次仍无结论时，记下现象与证据转下一个：判断不了记受阻，证据表明产品不符记失败。',
              '- 业务断言失败只记该场景失败并继续；连接异常、授权等待、未知遮挡或状态不明时停止本轮，剩余场景写成 not-executed 标记行（带 finding）。',
              '', '## 结果', '',
              '- 每执行完一个场景就往 results.jsonl 写一行，所选场景都要有；字段、证据与限制见提示词。']
    (directory / 'brief.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


class Executions:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store

    def waiting_merge(self, db, key, name):
        row = db.execute("SELECT id FROM tasks WHERE kind='merge' AND workspace=? AND batch=? "
                         f'AND status IN ({sql_states(OUTPUT_ACTIVE)}) ORDER BY id LIMIT 1', (key, name)).fetchone()
        return row['id'] if row else None

    def enqueue(self, key, name, scope=None, trigger='开发者发起', selection='pending', *, selected=None, session=False):
        with self.tasks.lock:
            manual = scope is None
            if manual:
                self.service.require_ready(key)
            with self.store.transaction() as db:
                if not manual and not self.service.auto_execute_enabled(db):
                    return None
                batch = self.service.batch(db, key, name)
                merge = None if session else self.waiting_merge(db, key, name)
                version = self.service.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                if manual:
                    product = self.service.workspace.product(self.service.workspace.aliases(document))
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                    batch = self.service.batch(db, key, name)
                    summary = calculate(db, batch)
                    if selected is not None:
                        available = {s['scenario'] for s in summary['scenarios'] if not s.get('held') and not s.get('waiting_change')}
                        if set(selected) - available:
                            reject(409, 'scenario-unavailable', 'scope', '场景不存在、待补充或等待修改场景：' + '、'.join(sorted(set(selected) - available)))
                        pending = {s['scenario']: s for s in pending_scope(db, batch, summary, product['product_digest'])}
                        scope = [pending.get(sid, dict(scenario=sid, reason='rerun')) for sid in selected]
                        selection = 'explicit'
                    else:
                        scope = ([dict(scenario=s['scenario'], reason='rerun') for s in summary['scenarios'] if not s.get('held') and not s.get('waiting_change')]
                                 if selection == 'all' else pending_scope(db, batch, summary, product['product_digest']))
                if not scope and not (manual and merge):
                    if manual:
                        reject(409, 'nothing-to-execute', 'scenarios', '尚无场景' if selection == 'all' else '没有待执行场景')
                    return None
                previous = db.execute("SELECT id,scope_json,input_json FROM tasks WHERE kind='execute' AND workspace=? AND batch=? "
                    f'AND status IN ({sql_states(EXECUTION_QUEUE)}) ORDER BY id LIMIT 1', (key, name)).fetchone()
                request = dict(selection=selection if manual else 'explicit', refresh_after_merge=bool(merge))
                status, error = (S.WAIT_MERGE, f'等待合并 #{merge}') if merge else (S.QUEUED, '')
                if previous:
                    prior = json.loads(previous['input_json'])
                    if selected is not None and (prior.get('selection') != 'explicit' or
                            {s['scenario'] for s in json.loads(previous['scope_json'])} - set(selected)):
                        reject(409, 'execution-queued', 'scope', f"执行任务 #{previous['id']} 已有其他范围，请先处理该任务后再执行所列场景")
                    request['selection'] = ('all' if 'all' in (prior.get('selection'), request['selection']) else
                                            'pending' if 'pending' in (prior.get('selection'), request['selection']) else 'explicit')
                    request['refresh_after_merge'] |= prior.get('refresh_after_merge', False)
                    combined = {s['scenario']: s for s in json.loads(previous['scope_json'])}
                    for scene in scope:
                        if combined.get(scene['scenario'], {}).get('reason') != 'rerun':
                            combined[scene['scenario']] = scene
                    task_id = previous['id']
                    db.execute('UPDATE tasks SET scope_json=?,input_json=? WHERE id=?',
                               (canonical(list(combined.values())), canonical(dict(prior, **request)), task_id))
                    if merge:
                        db.execute("UPDATE tasks SET status=?,error=?,snapshot_json='{}' WHERE id=?", (status, error, task_id))
                else:
                    task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,scope_json,created_at) "
                        "VALUES ('execute',?,?,?,?,?,?,?,?)",
                        (key, name, status, trigger, '', canonical(request), canonical(scope), now())).lastrowid
                    if error:
                        db.execute('UPDATE tasks SET error=? WHERE id=?', (error, task_id))
        return dict(ok=True, task=self.tasks.read(task_id))

    def freeze(self, task):
        workspace = self.service.workspace
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            version = self.service.latest_version(db, batch)
            if not version:
                raise ValueError('批次尚无场景')
            document = yaml.safe_load(version['content'])
            details = {s['scenario']: s for s in calculate(db, batch)['scenarios']}
            cases = self.service.case_union(db, batch)
            held = {h['case'] for h in read_holds(db, batch)['held']}
        scope = {s['scenario']: s['reason'] for s in json.loads(task['scope_json'])}
        scenes = [s for n in document['sops'] for s in n['scenarios'] if s['id'] in scope]
        if set(scope) != {s['id'] for s in scenes}:
            raise ValueError('执行范围中的场景已移除')
        aliases = workspace.aliases(document)
        product = workspace.product(aliases, files=True)
        files = product.pop('files')
        source = workspace.scenario_path(task['batch'])
        snapshot = dict(scenario_file_sha256=version['sha256'], scenarios={s['id']: fingerprint(s) for s in scenes},
                        config_digest=workspace.config_digest, skill_digest=workspace.skill_digest(), cases_digest=hashlib.sha256(canonical(cases).encode()).hexdigest(),
                        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else '', **product)
        previous = {sid: details[sid]['latest_agent'] for sid in scope if details[sid]['latest_agent']}
        request = json.loads(task['input_json'])
        inputs = dict(document=document, scenes=scenes, entries=workspace.execution_entries(), aliases=aliases, previous=previous,
                      cases=[c for c in cases if c['id'] not in held],
                      execution_origin='session' if self.tasks.kernel.claim(task) else 'board',
                      selection=request.get('selection', 'explicit'), refresh_after_merge=False)
        from .fixtures import definition
        if workspace.fixtures is not None:
            inputs['fixtures'] = definition(workspace)
        return snapshot, inputs, files

    def save_files(self, task, files):
        """准备时的产品文件清单单独存放在任务目录，登记时据此列出执行期间变化的文件。"""
        (self.tasks.directory(task['id']) / 'product-files.json').write_text(canonical(files), encoding='utf-8')

    def uncommitted(self, task, snapshot):
        """开始时未提交的文件及当时的内容记录（别名/相对路径），之后据此判断这些文件是否又有变化。"""
        path = self.tasks.directory(task['id']) / 'product-files.json'
        files = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        entries = {alias + '/' + item['path']: item for alias, items in files.items() for item in items}
        return [dict(entries.get(name, {}), path=name) for name in snapshot['changed']]

    def drift(self, task, snapshot, current, files):
        """准备后到登记前内容有变化的产品路径（别名/相对路径）。更早版本准备的任务没有文件清单，按仓库列出“别名/”。"""
        path = self.tasks.directory(task['id']) / 'product-files.json'
        if path.is_file():
            before = json.loads(path.read_text(encoding='utf-8'))
            return [alias + '/' + name for alias, names in sorted(changed_files(before, files).items()) for name in names]
        return [alias + '/' for alias, repo in sorted(snapshot['repos'].items())
                if current['repos'].get(alias, {}).get('digest') != repo['digest']]

    @staticmethod
    def counts(rows):
        """沿用与重跑分开计数：重跑是实际执行并得出通过或失败的场景，受阻、跳过不计入两者。"""
        reused = [isinstance(r.get('note'), str) and r['note'].startswith('沿用：') for r in rows]
        return dict(reused_count=sum(reused),
                    rerun_count=sum(not used and r.get('status') in ('pass', 'fail') for used, r in zip(reused, rows)))

    def committed(self, task):
        """被阻挡期间只核对产品是否已无未提交改动；开始前仍按 freeze 完整冻结并核对输入。"""
        workspace = self.service.workspace
        try:
            with self.store.connection() as db:
                version = self.service.latest_version(db, self.service.batch(db, task['workspace'], task['batch']))
            if not version:
                return False
            return not workspace.product(workspace.aliases(yaml.safe_load(version['content'])))['dirty']
        except (OSError, ValueError, KeyError, yaml.YAMLError, APIError, subprocess.SubprocessError):
            return False

    def prepare(self, task):
        with self.store.connection() as db:
            merge = self.waiting_merge(db, task['workspace'], task['batch'])
        if merge:
            request = json.loads(task['input_json'])
            if (task['status'] != S.WAIT_MERGE or task['error'] != f'等待合并 #{merge}'
                    or task['snapshot_json'] != '{}' or not request.get('refresh_after_merge')):
                request['refresh_after_merge'] = True
                self.tasks.update(task['id'], status=S.WAIT_MERGE, error=f'等待合并 #{merge}',
                                  input_json=canonical(request), snapshot_json='{}')
            (self.tasks.directory(task['id']) / 'snapshot.json').unlink(missing_ok=True)
            if self.tasks.browser_owner == task['id']:
                self.tasks.browser_owner = None
            self.tasks.kernel.wake(task['id'])
            return False
        if not self.tasks.kernel.check_due(task, self.tasks.types['execute'].waiting):
            return False
        # 已排队任务可能尚未观察到一次很快结束的合并，准备前仍须排除最新暂缓项。
        with self.store.transaction() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            held = held_scenarios(read_holds(db, batch))
            original_scope = json.loads(task['scope_json'])
            available = [s for s in original_scope if s['scenario'] not in held]
            if available != original_scope:
                task['scope_json'] = canonical(available)
                db.execute('UPDATE tasks SET scope_json=? WHERE id=?', (task['scope_json'], task['id']))
        if original_scope and not available and not json.loads(task['input_json']).get('refresh_after_merge'):
            self.tasks.update(task['id'], status=S.COMPLETED, finished_at=now(), error='执行范围的用例全部暂缓，等待补充')
            return False
        if not self.service.environment.approved():
            self.tasks.update(task['id'], status=S.QUEUED, error='尚未搭建或自检未通过，等待工作区就绪')
            return False
        declarations = self.service.entries(task['workspace'])
        if not declarations['ok']:
            self.tasks.update(task['id'], status=S.QUEUED, error='入口声明不合格')
            return False
        if self.service.environment.state.get('waiting_port'):
            self.tasks.update(task['id'], status=S.QUEUED, error=self.service.environment.state['status'])
            return False
        request = json.loads(task['input_json'])
        if request.get('refresh_after_merge'):
            with self.store.transaction() as db:
                batch = self.service.batch(db, task['workspace'], task['batch'])
                version = self.service.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                product = self.service.workspace.product(self.service.workspace.aliases(document))
                db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                batch = self.service.batch(db, task['workspace'], task['batch'])
                summary = calculate(db, batch)
                if request.get('selection') == 'all':
                    scope = [dict(scenario=s['scenario'], reason='rerun') for s in summary['scenarios'] if not s.get('held') and not s.get('waiting_change')]
                else:
                    current = {s['scenario'] for s in summary['scenarios'] if not s.get('held') and not s.get('waiting_change')}
                    scope = ({s['scenario']: s for s in pending_scope(db, batch, summary, product['product_digest'])}
                             if request.get('selection') == 'pending' else {})
                    for scene in json.loads(task['scope_json']):
                        if scene['scenario'] in current and (scene['reason'] == 'rerun' or scene['scenario'] not in scope):
                            scope[scene['scenario']] = scene
                    scope = list(scope.values())
                request['refresh_after_merge'] = False
                task['scope_json'], task['input_json'] = canonical(scope), canonical(request)
                db.execute(f"UPDATE tasks SET scope_json=?,input_json=?,status='{S.QUEUED}',error='' WHERE id=?",
                           (task['scope_json'], task['input_json'], task['id']))
            if not scope:
                self.tasks.update(task['id'], status=S.COMPLETED, finished_at=now(), error='合并结束，当前版本没有待执行场景')
                return False
        snapshot, inputs, files = self.freeze(task)
        if snapshot['dirty']:
            self.tasks.update(task['id'], status=S.WAIT_COMMIT, error='工作区有未提交的产品改动：' + '、'.join(snapshot['changed'][:10]))
            return False
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'snapshot.json').write_text(canonical(snapshot), encoding='utf-8')
        self.save_files(task, files)
        task['snapshot_json'], task['input_json'] = canonical(snapshot), canonical(inputs)
        self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'], error='')
        return True

    def changes(self, task, snapshot, *, started=False):
        """返回作废原因与执行期间变化的产品路径。开始执行后（started）产品内容与提交号的变化不作废：
        结果按准备时的产品快照登记并列出变化的路径，这些通过记录此后都须判定影响（见 statistics.stale）。"""
        self.service.workspace.reload()
        if self.service.workspace.skill_changed(snapshot):
            return '验证技能变化', []
        if not self.service.environment.approved() or snapshot['config_digest'] != self.service.workspace.config_digest:
            return '工作区配置或自检状态变化', []
        current, _, files = self.freeze(task)
        changed = self.tasks.types['execute'].inputs(task, product=not started).changed(current)
        if changed:
            return '、'.join(changed) + '变化', []
        return '', self.drift(task, snapshot, current, files) if started else []

    SCENARIO_LEVEL = {'场景版本', '场景内容', '用例并集', '工作区场景文件'}

    def scenario_changes(self, task, snapshot):
        """执行期间只有场景或用例变化时（D96），返回内容变了的场景（含引用变化用例的场景）与产品变化路径；
        技能、配置、自检或其他输入变化返回 None，仍整轮作废。"""
        self.service.workspace.reload()
        if (self.service.workspace.skill_changed(snapshot) or not self.service.environment.approved()
                or snapshot['config_digest'] != self.service.workspace.config_digest):
            return None
        try:
            current, current_inputs, files = self.freeze(task)
        except (ValueError, KeyError, APIError):
            return None
        changed = self.tasks.types['execute'].inputs(task, product=False).changed(current)
        if not changed or not set(changed) <= self.SCENARIO_LEVEL:
            return None
        frozen = json.loads(task['input_json'])
        before = {case['id']: canonical(case) for case in frozen.get('cases', [])}
        after = {case['id']: canonical(case) for case in current_inputs.get('cases', [])}
        cases = {cid for cid in before.keys() | after.keys() if before.get(cid) != after.get(cid)}
        voided = {sid for sid, value in snapshot['scenarios'].items() if current['scenarios'].get(sid) != value}
        voided |= {scene['id'] for scene in frozen.get('scenes', []) if set(scene.get('refs', [])) & cases}
        return sorted(voided), self.drift(task, snapshot, current, files)

    @staticmethod
    def moved(result):
        """执行期间产品内容有变化的仓库与本轮收下的地图反馈；没有时不写对应字段。"""
        extras = dict(product_changed=result['product_changed']) if result.get('product_changed') else {}
        if result.get('voided_scenarios'):
            extras['voided_scenarios'] = result['voided_scenarios']
        if result.get('not_executed'):
            extras['not_executed'] = result['not_executed']
        if result.get('atlas_feedback'):
            extras['atlas_feedback'] = result['atlas_feedback']
        return extras

    def materialize(self, task, inputs, environment):
        directory = self.tasks.directory(task['id'])
        from .fixtures import materialize
        materialize(self.service, directory, [task['batch']], inputs.get('fixtures'))
        selected = {s['id'] for s in inputs['scenes']}
        document = dict(inputs['document'], sops=[dict(n, scenarios=[s for s in n['scenarios'] if s['id'] in selected])
                         for n in inputs['document']['sops'] if any(s['id'] in selected for s in n['scenarios'])])
        (directory / 'scenarios.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
        web_scenes = [s for s in inputs['scenes'] if inputs['entries'][s['verify']]['surface'] == 'web' and inputs['entries'][s['verify']].get('driver') in (None, 'playwright-acceptance')]
        write_scope(directory, task['workspace'], task['batch'], directory / 'scenarios.yaml', web_scenes, entries=inputs['entries'])
        (directory / 'web-pending.json').write_text(canonical([dict(scenario=s['id']) for s in web_scenes]), encoding='utf-8')
        addresses = {}
        for name, entry in inputs['entries'].items():
            variable = entry.get('address_env')
            if variable and variable not in credential_names(inputs['entries']):
                if not environment.get(variable, '').strip():
                    raise ValueError('入口 ' + name + ' 缺少地址环境变量 ' + variable)
                addresses[name] = environment[variable]
        (directory / 'entries.json').write_text(canonical(dict(entries=inputs['entries'], addresses=addresses)), encoding='utf-8')
        referenced = {ref for scene in inputs['scenes'] for ref in scene.get('refs', [])}
        (directory / 'cases.json').write_text(canonical([case for case in inputs['cases'] if case['id'] in referenced]), encoding='utf-8')
        (directory / 'pending.json').write_text(task['scope_json'], encoding='utf-8')
        (directory / 'previous-results.json').write_text(canonical(inputs['previous']), encoding='utf-8')
        prints = json.loads(task['snapshot_json']).get('scenarios', {})
        atlas_info = self.tasks.atlas.materialize_execution(directory, task['batch'],
            [dict(id=s['id'], verify=s['verify'], fingerprint=prints.get(s['id'])) for s in inputs['scenes']])
        from .identities import materialize as identities_materialize
        has_identities = identities_materialize(self.service.workspace, self.store, directory, {s['verify'] for s in inputs['scenes']})
        write_brief(directory, json.loads(task['scope_json']), inputs['entries'], atlas_info, inputs.get('fixtures') is not None,
                    task_id=task['id'], identities=has_identities)
        with self.store.connection() as db:
            from .regressions import read_links
            batch = self.service.batch(db, task['workspace'], task['batch'])
            (directory / 'regression-links.json').write_text(canonical(read_links(db, batch, local=True)), encoding='utf-8')
        (directory / 'evidence').mkdir(exist_ok=True)
        snapshot = json.loads(task['snapshot_json'])
        changes = {}
        rounds = {}
        with self.store.connection() as db:
            for row in inputs['previous'].values():
                rounds[row['round_id']] = tuple(db.execute("SELECT r.product_commit,COALESCE(c.uncommitted_json,'[]') FROM rounds r "
                    'LEFT JOIN round_product_changes c ON c.round_id=r.id WHERE r.id=?', (row['round_id'],)).fetchone())
        current = {alias + '/' + item['path']: item for alias, items in
                   json.loads((directory / 'product-files.json').read_text(encoding='utf-8')).items() for item in items}
        content = lambda item: item and {k: v for k, v in item.items() if k != 'path'}
        for alias in inputs['aliases']:
            root = self.service.workspace.repos[alias]
            prior, unseen = {}, set()
            for commit, uncommitted in rounds.values():
                for item in commit.split(', '):
                    name, separator, sha = item.partition(':')
                    if separator and name == alias:
                        prior[sha] = True
                # 上次结果执行时未提交的文件不在提交差异里；内容与当时不同的列出，交给影响判定。
                unseen |= {item['path'][len(alias) + 1:] for item in json.loads(uncommitted)
                           if item['path'].startswith(alias + '/') and content(item) != content(current.get(item['path']))}
            changes[alias] = dict(current=snapshot['repos'][alias]['commit'], directory=str(root), previous=[])
            if unseen:
                changes[alias]['previous_uncommitted'] = sorted(unseen)
            if snapshot.get('dirty'):
                changes[alias]['uncommitted'] = [p[len(alias) + 1:] for p in snapshot['changed'] if p.startswith(alias + '/')]
            for sha in prior:
                try:
                    target = [] if snapshot.get('dirty') else [snapshot['repos'][alias]['commit']]
                    diff = git(root, 'diff', '--name-status', sha, *target, '--').decode()
                except subprocess.SubprocessError:
                    diff = '旧提交不可读取，不能仅据此沿用结果'
                changes[alias]['previous'].append(dict(commit=sha, files=diff))
        (directory / 'changes.json').write_text(canonical(changes), encoding='utf-8')
        origin = inputs.get('execution_origin','session' if self.tasks.kernel.claim(task) else 'board')
        (directory/'origin.json').write_text(canonical(dict(execution_origin=origin)),encoding='utf-8')
        (directory/'repos.json').write_text(canonical({alias:str(path) for alias,path in self.service.workspace.repos.items()}),encoding='utf-8')
        external = origin == 'session'
        prompt = Template(files('sop_board').joinpath('prompts/execute.md').read_text(encoding='utf-8')).substitute(task_directory=directory, skill_path=self.service.workspace.skill,
            environment_preparation=('由执行会话按项目验证技能与手册准备入口与隔离数据；按 entries.json 的 operate、observe 和 evidence 操作。'
                '结束前按手册停止本轮由自己启动的服务进程并核对已退出，只清理本轮获授权的资源，不停止使用者已有的服务或浏览器。'
                '在结果 note 中列出已停止的服务名称、PID 或进程组及停止结果；没有启动服务时写“本轮未启动服务”，停止失败时如实列出仍运行的进程与原因。' if external else
                '看板已经用工作区配置启动了实际环境；按 entries.json 的 operate、observe 和 evidence 操作。'),
            repository_instruction=('地址从 entries.json 的 addresses 取得；各仓库目录从任务目录的 repos.json 按仓库别名读取。' if external else
                '地址从 entries.json 的 addresses 取得；各仓库目录由 SOP_REPO_<别名> 环境变量提供。'),
            environment_origin='执行会话自身' if external else '启动看板',
            environment_restriction='' if external else '不启动或替换环境，',
            playwright_skill=driver_skill() / 'SKILL.md')
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def read_results(self, task, scenes, session, batch=None, *, partial=False):
        """partial=True 时接受 status 为 not-executed 的标记行（须有 finding、无证据）：中途停止后未执行的场景，
        不登记、留在待执行范围（D87）。复验须完整执行，不接受标记行。"""
        directory = self.tasks.directory(task['id'])
        path = directory / 'results.jsonl'
        if not path.is_file() or path.is_symlink():
            raise ValueError('results.jsonl 不存在')
        raw = [json.loads(line, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
               for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        selected = {s['id'] for s in scenes}
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task.get('input_json', '{}'))
        scope = {s['scenario']: s['reason'] for s in json.loads(task.get('scope_json', '[]'))}
        changed = {sid for sid in selected if scope.get(sid) == 'source-changed'}
        impact = {}
        if changed:
            label = '、'.join(sorted(changed))
            impact_path = directory / 'impact.json'
            if not impact_path.is_file() or impact_path.is_symlink():
                raise ValueError(label + ' 缺少 impact.json 影响判定')
            try:
                decisions = json.loads(impact_path.read_text(encoding='utf-8'), object_pairs_hook=judgment_object,
                                       parse_constant=invalid_judgment_constant)
            except (OSError, ValueError) as error:
                raise ValueError(label + ' impact.json 不合格：' + str(error)) from error
            if not isinstance(decisions, list):
                raise ValueError(label + ' impact.json 应为数组')
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError(label + ' impact.json 每项应为对象')
                sid = decision.get('scenario')
                if not isinstance(sid, str) or sid not in changed or sid in impact:
                    raise ValueError('impact.json 场景重复或不在待判定范围内：' + str(sid))
                if type(decision.get('affected')) is not bool or not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
                    raise ValueError(sid + ' impact.json 必须有布尔值 affected 和非空 reason')
                impact[sid] = decision['affected']
            if changed - impact.keys():
                raise ValueError('、'.join(sorted(changed - impact.keys())) + ' 缺少 impact.json 影响判定')
        rows, seen = [], set()
        for row in raw:
            if not isinstance(row, dict):
                raise ValueError('结果行应为对象')
            if batch is not None and row.get('batch') != batch:
                continue
            sid = row.get('scenario')
            if not isinstance(sid, str) or sid not in selected or sid in seen:
                raise ValueError('结果重复或不在本轮范围内：' + str(sid))
            seen.add(sid)
            note = row.get('note') or ''
            reused = isinstance(note, str) and note.startswith('沿用：')
            if sid in impact and impact[sid] == reused:
                raise ValueError(sid + ' impact.json 判定与结果不一致：' +
                                 ('判定受影响，必须实际执行' if impact[sid] else '判定不受影响，必须沿用'))
            if row.get('status') == 'not-executed':
                if not partial:
                    raise ValueError(sid + ' 复验须完整执行，不接受未执行标记')
                if not isinstance(row.get('finding'), str) or not row['finding'].strip() or row.get('evidence'):
                    raise ValueError(sid + ' 未执行标记须写 finding 说明原因，且不附证据')
                rows.append(dict(scenario=sid, status='not-executed', finding=row['finding'].strip()))
                continue
            if scope.get(sid) == 'rerun' and isinstance(note, str) and note.startswith('沿用：'):
                raise ValueError(sid + ' 须实际执行并保存新证据，不得沿用')
            if 'issue' in row and (type(row['issue']) is not int or row['issue'] < 1):
                raise ValueError(sid + ' issue 应为问题台账编号（正整数）')
            evidence = row.get('evidence')
            if not isinstance(evidence, list):
                raise ValueError(sid + ' evidence 应为路径数组')
            prepared = []
            for name in evidence:
                if not isinstance(name, str):
                    raise ValueError('证据路径应为字符串')
                p = directory / name
                base = directory / 'evidence'
                if Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith('evidence/') or base.is_symlink() or not p.resolve().is_relative_to(base.resolve()) or not base.resolve().is_relative_to(directory):
                    raise ValueError(sid + ' 证据路径不在 evidence/ 下')
                if not p.is_file():
                    raise ValueError(sid + ' 证据文件不存在')
                prepared.append(p)
            if scope.get(sid) == 'rerun' and row.get('status') in ('pass','fail') and not prepared:
                raise ValueError(sid + ' 须实际执行并保存新证据')
            if isinstance(note, str) and note.startswith('沿用：'):
                previous = inputs.get('previous', {}).get(sid)
                if (scope.get(sid) != 'source-changed' or not previous or previous['status'] != 'pass'
                        or row.get('status') != 'pass' or not note[3:].strip()
                        or previous['fingerprint'] != snapshot['scenarios'][sid]):
                    raise ValueError(sid + ' 不符合沿用条件，须重跑；沿用必须保留原通过状态并说明理由')
                prepared += previous['evidence']
            row.update(fingerprint=snapshot['scenarios'][sid], executor=session, session=str(task['id']), evidence=prepared)
            rows.append(row)
        if selected - seen:
            raise ValueError('、'.join(sorted(selected - seen)) + ' 缺少结果行')
        return rows

    def blocked_impact(self, task, blocked):
        """保留完整影响判定清单；凭据缺失项不能以原通过结果沿用。"""
        if not blocked:
            return
        path = self.tasks.directory(task['id']) / 'impact.json'
        if path.is_symlink():
            raise ValueError('impact.json 不能是符号链接')
        changed = {row['scenario'] for row in json.loads(task['scope_json']) if row['reason'] == 'source-changed'}
        generated = [dict(scenario=row['scenario'], affected=True,
                         reason='缺少凭据，尚无法证明不受影响，待环境就绪后经真实入口执行；' + row['finding'])
                     for row in blocked if row['scenario'] in changed]
        if generated:
            previous = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=judgment_object,
                                  parse_constant=invalid_judgment_constant) if path.is_file() else []
            expected = changed - {row['scenario'] for row in generated}
            if (not isinstance(previous, list) or any(not isinstance(row, dict) or not isinstance(row.get('scenario'), str) for row in previous)
                    or len(previous) != len(expected) or {row['scenario'] for row in previous} != expected):
                raise ValueError('impact.json 与本次可执行场景的影响判定范围不一致')
            path.write_text(canonical(previous + generated), encoding='utf-8')
        elif not path.exists():
            path.write_text('[]', encoding='utf-8')

    def consume(self, task, session, *, runnable=None, blocked=None, scoped=None, implementer=None, execution_origin='board'):
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task['input_json'])
        blocked = blocked or []
        runnable = inputs['scenes'] if runnable is None else runnable
        scoped = task if scoped is None else scoped
        with self.tasks.lock:
            receipt = self.tasks.kernel.receipt(task, phase='execute')
            if receipt is not None:
                return dict(rounds=receipt['rounds'], rows=receipt['rows'], **self.moved(receipt))
            reason, _ = self.changes(task, snapshot, started=True)
            if reason and self.scenario_changes(task, snapshot) is None:
                raise InputChanged(reason)
            rows = self.read_results(scoped, runnable, session, partial=True) if runnable else []
            not_executed = [row['scenario'] for row in rows if row['status'] == 'not-executed']
            rows = [row for row in rows if row['status'] != 'not-executed']
            if not rows and not blocked:
                raise ValueError('本轮没有执行任何场景（' + '、'.join(not_executed) + ' 都标为未执行）；不登记轮次，请重新执行')
            from .issues import check_result_issues
            with self.store.connection() as db:
                check_result_issues(db, task['workspace'], rows)
            self.blocked_impact(task, blocked)
            append_blocked(self.tasks.directory(task['id']), blocked)
            rows.extend(dict(row, fingerprint=snapshot['scenarios'][row['scenario']], session=str(task['id'])) for row in blocked)
            if execution_origin == 'session':
                for row in rows:
                    row.update(executor=session, session=session)
            with self.store.transaction() as db:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] not in (S.RUNNING, S.SESSION):
                    return dict(rounds=[], rows=[])
                reason, moved = self.changes(task, snapshot, started=True)
                voided = []
                if reason:
                    partial = self.scenario_changes(task, snapshot)
                    if partial is None or {row['scenario'] for row in rows} <= set(partial[0]):
                        raise InputChanged(reason)
                    voided, moved = partial
                    rows = [row for row in rows if row['scenario'] not in voided]
                batch = self.service.batch(db, task['workspace'], task['batch'])
                version = self.service.latest_version(db, batch)
                key = f"execute-{task['id']}-{task['attempt']}"
                existing = db.execute('SELECT number FROM rounds WHERE batch=? AND idempotency_key=?', (batch['id'], key)).fetchone()
                for row in rows:
                    row['evidence'] = [p if isinstance(p, dict) else dict(name=p.name,
                        sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
                product = dict(commit=snapshot['commit'], digest=snapshot['product_digest'],
                               dirty=snapshot['dirty'] if execution_origin == 'session' else False)
                if moved:
                    product['changed'] = moved
                if product['dirty']:
                    product['uncommitted'] = self.uncommitted(task, snapshot)
                body = dict(idempotency_key=key, scenario_file_sha256=version['sha256'] if voided else snapshot['scenario_file_sha256'],
                    submitted_by=session, execution_origin=execution_origin, product=product, results=rows)
                if implementer is not None:
                    body['implementer'] = implementer
                if existing:
                    rounds = [existing['number']]
                else:
                    rounds = [self.service.register_round(db, batch, version, body, skill_digest=snapshot['skill_digest'])]
                from .fixtures import link_results
                link_results(db, task['id'], batch, rounds[0], self.tasks.directory(task['id']))
                from .atlas import record_round
                record_round(db, batch['id'], rounds[0], self.tasks.directory(task['id']))
                from .issues import link_results as link_issues
                link_issues(db, task['workspace'], batch, rounds[0], rows)
                result = dict(rounds=rounds, rows=rows, session=session)
                if moved:
                    result['product_changed'] = moved
                if voided:
                    result['voided_scenarios'] = voided
                if not_executed:
                    result['not_executed'] = sorted(not_executed)
                feedback = self.tasks.atlas.collect_feedback(db, task['id'], self.tasks.directory(task['id']),
                                                             scenarios={row['scenario'] for row in rows})
                if feedback is not None:
                    result['atlas_feedback'] = feedback
                self.tasks.kernel.register(task, result, connection=db, phase='execute')
                return result

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        error = void_reason = session = ''
        rounds = []
        rows = []
        moved = {}
        process = None
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task['input_json'])
        try:
            with self.tasks.lock:
                self.tasks.starting = None
            declared = credential_names({'fixtures': inputs.get('fixtures')})
            preflight = self.service.environment.agent_environment(self.service.workspace.repos, credential_names=declared)
            blocked = blocked_results(inputs['scenes'], inputs['entries'], preflight)
            blocked_ids = {row['scenario'] for row in blocked}
            runnable = [scene for scene in inputs['scenes'] if scene['id'] not in blocked_ids]
            scoped = dict(task, scope_json=canonical([row for row in json.loads(task['scope_json']) if row['scenario'] not in blocked_ids]))
            context = self.service.environment.use_baseline() if runnable else nullcontext(preflight)
            with context as environment:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != S.RUNNING:
                    return
                # 开始前快照须与刚启动的环境一致；开始之后的产品变化不再作废。
                void_reason, _ = self.changes(task, snapshot)
                if void_reason:
                    return
                (directory / 'credential-blocked.json').write_text(canonical(blocked), encoding='utf-8')
                if runnable:
                    # 环境可能在准备后复用；补齐本轮冻结的配方凭据，仍只取看板启动环境。
                    environment = self.service.environment.agent_environment(self.service.workspace.repos, credential_names=declared)
                    prompt = self.tasks.types['execute'].prepare(scoped, inputs=dict(inputs, scenes=runnable), environment=environment)
                    with self.tasks.lock:
                        process = self.tasks.kernel.launch(task, directory, prompt, self.tasks.types['execute'].launch(task, environment=environment))
                        self.tasks.processes[task['id']] = process
                    code = wait_codex(process, self.tasks.timeout)
                    if code:
                        raise ValueError(codex_failure(directory, code))
                    session = session_id(directory, '')
                    if not session:
                        raise ValueError('Codex 没有返回会话编号')
                else:
                    session = 'Flow Board 入口检查'
                consumed = self.tasks.types['execute'].consume(task, session, runnable=runnable, blocked=blocked, scoped=scoped)
                rounds, rows, moved = consumed['rounds'], consumed['rows'], self.moved(consumed)
        except InputChanged as problem:
            void_reason = str(problem)
        except APIError as problem:
            error = '；'.join(p['problem'] for p in problem.problems)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as problem:
            error = str(problem)
        finally:
            if process:
                terminate_group(process)
                self.service.workspace.reload()
                if not rounds and self.service.workspace.skill_changed(snapshot):
                    void_reason = '验证技能变化'
                session = session_id(directory, session)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
                if not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == S.RUNNING:
                    result = dict(ok=not error and not void_reason, problems=[error] if error else [], **self.counts(rows), **moved)
                    (directory / 'result.json').write_text(canonical(result), encoding='utf-8')
                    self.tasks.update(task['id'], status=S.VOID if void_reason else S.FAILED if error else S.COMPLETED,
                        finished_at=now(), error=error, session_id=session or None, void_reason=void_reason,
                        rounds_json=canonical(rounds), result_json=canonical(result))
