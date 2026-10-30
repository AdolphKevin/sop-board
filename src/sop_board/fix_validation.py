"""在各仓修复目录启动适配，由新会话复测问题与所有可能受影响场景。"""

import json
from contextlib import nullcontext
from importlib.resources import files
from string import Template
import subprocess

import yaml

from .kernel import (InputChanged, S, codex_failure, session_id, terminate_group, wait_codex)

from .errors import APIError
from .executions import Executions
from .service import canonical, fingerprint
from .validation import validate_round
from .playwright import driver_skill, write_scope
from .credentials import append_blocked, blocked_results, names as credential_names
from .repair_repositories import head


class ValidationContext:
    def __init__(self, tasks, directory):
        self.tasks, self.path = tasks, directory

    def __getattr__(self, name):
        return getattr(self.tasks, name)

    def directory(self, task_id):
        return self.path


class FixValidation:
    def __init__(self, fixes):
        self.fixes, self.tasks, self.store = fixes, fixes.tasks, fixes.store

    def prepare(self, task, runtime, environment):
        workspace = self.fixes.service.workspace
        directory, snapshot = runtime['directory'], runtime['snapshot']
        runnable, blocked, info = runtime['runnable'], runtime['blocked'], runtime['info']
        entries = snapshot.get('execution_entries', workspace.execution_entries())
        from .fixtures import materialize
        materialize(self.fixes.service, directory, {s['batch'] for s in runtime['selected']}, snapshot.get('fixtures'))
        addresses = {name: environment[entry['address_env']] for name, entry in entries.items()
            if entry.get('address_env') and entry['address_env'] in environment and entry['address_env'] not in credential_names(entries)}
        (directory / 'credential-blocked.json').write_text(canonical(blocked), encoding='utf-8')
        (directory / 'retest.json').write_text(canonical([dict(batch=s['batch'], scenario=s['id'], scene={k:v for k,v in s.items() if k != 'batch'}) for s in runnable]), encoding='utf-8')
        batches = {s['batch'] for s in runnable}
        if len(batches) == 1:
            # 单批次复验同样提供地图节选、绑定与速查卡；跨批次复验按地图索引与 atlas show 查找（D86）。
            from .service import fingerprint
            from .executions import write_brief
            info_atlas = self.tasks.atlas.materialize_execution(directory, next(iter(batches)),
                [dict(id=s['id'], verify=s['verify'], fingerprint=fingerprint({k: v for k, v in s.items() if k != 'batch'})) for s in runnable])
            write_brief(directory, [dict(scenario=s['id'], reason='rerun') for s in runnable], entries, info_atlas,
                        snapshot.get('fixtures') is not None, task_id=task['id'])
        (directory / 'entries.json').write_text(canonical(dict(entries=entries, addresses=addresses)), encoding='utf-8')
        (directory / 'snapshot.json').write_text(canonical(dict(snapshot, repositories=info['repos'])), encoding='utf-8')
        (directory / 'repos.json').write_text(canonical({alias:str(path) for alias,path in runtime['directories'].items()}),encoding='utf-8')
        (directory / 'cases.json').write_text(canonical(snapshot['cases']), encoding='utf-8')
        web_scopes = []
        for index, batch in enumerate(dict.fromkeys(scene['batch'] for scene in runnable), 1):
            batch_scenes = [scene for scene in runnable if scene['batch'] == batch and entries[scene['verify']]['surface'] == 'web' and entries[scene['verify']].get('driver') in (None, 'playwright-acceptance')]
            if not batch_scenes:
                continue
            folder = directory / 'playwright' / str(index)
            folder.mkdir(parents=True, exist_ok=True)
            document = yaml.safe_load(snapshot['batches'][batch]['content'])
            ids = {scene['id'] for scene in batch_scenes}
            document['sops'] = [dict(node, scenarios=[scene for scene in node['scenarios'] if scene['id'] in ids])
                for node in document['sops'] if any(scene['id'] in ids for scene in node['scenarios'])]
            source = folder / 'scenarios.yaml'
            source.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
            scope_file = write_scope(folder, task['workspace'], batch, source, batch_scenes, entries=entries)
            web_scopes.append(dict(batch=batch, scope=str(scope_file), scenarios=str(source)))
        (directory / 'playwright-scopes.json').write_text(canonical(web_scopes), encoding='utf-8')
        prompt = Template(files('sop_board').joinpath('prompts/fix-verify.md').read_text(encoding='utf-8')).substitute(task_directory=directory, skill_path=workspace.skill,
            validation_instruction=('你是未参与修复的新会话，独立复验开发者当前工作区中的产品修复，产品可能尚未提交。' if self.tasks.kernel.claim(task) else
                '你是新会话，独立复测已提交的产品修复，不参与修复。'),
            environment_instruction=('各仓库目录从任务目录的 repos.json 按仓库别名读取。snapshot.json 的 session_files 是复验准备时、修复输出已登记后的产品文件清单，不是修复前清单。'
                '按项目验证技能与手册准备并核对这些仓库对应的入口环境；看板不替本会话启停环境。'
                '结束前按手册停止本轮由自己启动的服务进程并核对已退出，只清理本轮获授权的资源，不停止使用者已有的服务或浏览器。'
                '在结果 note 中列出已停止的服务名称、PID 或进程组及停止结果；没有启动服务时写“本轮未启动服务”，停止失败时如实列出仍运行的进程与原因。' if self.tasks.kernel.claim(task) else
                '看板已经停止基线环境，并通过 adapter.env 启动包含各仓修复工作树的同一套环境；不要自行启停环境。'),
            environment_origin='本会话' if self.tasks.kernel.claim(task) else '启动看板',
            playwright_skill=driver_skill() / 'SKILL.md')
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def input_change(self, task, info, aliases, directories):
        """复验依据的场景、用例、配置或产品代码变化时返回原因；输入变化不是复测未通过（SOP-BOARD-5）。"""
        workspace = self.fixes.service.workspace
        workspace.reload()
        stale = self.fixes.snapshot_changes(task)
        if stale:
            return stale
        if self.tasks.validation_type.inputs(task, product=info).changed(workspace.product(aliases, directories)):
            return '复测期间仓库代码变化'
        return ''

    def commit_change(self, task):
        """修复提交登记后，工作树又被提交或移动时返回原因；复验依据的产品已不是这次修复。"""
        with self.store.connection() as db:
            attempt = db.execute('SELECT commits_json,start_commits_json FROM fix_attempts WHERE task=? ORDER BY number DESC LIMIT 1',
                                 (task['id'],)).fetchone()
        if attempt is None:
            return ''
        commits, starts = json.loads(attempt['commits_json']), json.loads(attempt['start_commits_json'])
        moved = [repo['alias'] for repo in self.fixes.repositories.rows(task['id'])
                 if (repo['repair_commit'] or commits.get(repo['alias'], starts.get(repo['alias'])))
                 and head(repo['worktree']) != (repo['repair_commit'] or commits.get(repo['alias'], starts.get(repo['alias'])))]
        return '修复提交后、复验开始前产品发生变化：' + '、'.join(moved) if moved else ''

    def read_output(self, task, runtime, session):
        if not session or session.casefold().strip() == (task.get('session_id') or '').casefold().strip():
            raise ValueError('复验须由未参与修复的新会话执行，执行者不能与修复会话相同')
        workspace = self.fixes.service.workspace
        directory, snapshot = runtime['directory'], runtime['snapshot']
        runnable, blocked, info = runtime['runnable'], runtime['blocked'], runtime['info']
        rows, selected, aliases, directories = runtime['rows'], runtime['selected'], runtime['aliases'], runtime['directories']
        if workspace.skill_changed(snapshot):
            raise ValueError('验证技能变化')
        executions = Executions(ValidationContext(self.tasks, directory))
        for batch in dict.fromkeys(s['batch'] for s in runnable):
            group = [s for s in runnable if s['batch'] == batch]
            bound = dict(task, input_json='{}', scope_json='[]', snapshot_json=canonical(dict(scenarios={s['id']: fingerprint(s) for s in group})))
            rows.extend(executions.read_results(bound, group, session, batch=batch))
        append_blocked(directory, blocked)
        fingerprints = {(s['batch'], s['id']): fingerprint(s) for s in selected}
        rows.extend(dict(row, fingerprint=fingerprints[(row['batch'], row['scenario'])], session=str(task['id'])) for row in blocked)
        raw = [json.loads(line) for line in (directory / 'results.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        if any((r.get('batch'), r.get('scenario')) not in {(s['batch'], s['id']) for s in selected} for r in raw):
            raise ValueError('复测结果有范围外批次或场景')
        changed = self.input_change(task, info, aliases, directories)
        if workspace.skill_changed(snapshot):
            raise ValueError('验证技能变化')
        if changed:
            # 输入变化不是复测未通过，按 SOP-BOARD-5 不计入连续三次的上限。
            raise InputChanged(changed)
        failed = [r for r in rows if r.get('status') != 'pass']
        reason = '；'.join(str(r.get('actual', '')) for r in failed) or ('存在未通过场景' if failed else '')
        return reason

    def register(self, task, runtime, session, reason, *, connection=None):
        workspace = self.fixes.service.workspace
        directory, snapshot, info = runtime['directory'], runtime['snapshot'], runtime['info']
        rows, selected, number, sequence = runtime['rows'], runtime['selected'], runtime['number'], runtime['sequence']
        evidence = [str(p) for p in directory.rglob('*') if p.is_file() and not p.is_symlink()]
        with self.tasks.lock, (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            phase = 'fix-validation-' + str(sequence)
            receipt = self.tasks.kernel.receipt(task, connection=db, phase=phase, number=number)
            if receipt is not None:
                return receipt
            workspace.reload()
            if workspace.skill_changed(snapshot):
                return dict(status='void', reason='验证技能变化', evidence=evidence, scenarios=[], skill_digest=snapshot['skill_digest'])
            if 'aliases' in runtime:
                # 已持有写事务：同时到达的用例上传、场景推送须等本次登记结束，检查与登记之间不会插入输入变化。
                try:
                    changed = self.input_change(task, info, runtime['aliases'], runtime['directories'])
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    changed = '登记前无法重新核对输入：' + str(error)
                if changed:
                    return dict(status='void', reason=changed, evidence=evidence, scenarios=[])
            for row in rows:
                row['evidence'] = [dict(name=p.name, sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
            for batch in dict.fromkeys(s['batch'] for s in selected):
                group = [r for r in rows if r['batch'] == batch]
                version = db.execute('SELECT id FROM versions WHERE batch=(SELECT id FROM batches WHERE workspace=? AND name=?) AND sha256=? ORDER BY id DESC LIMIT 1',
                    (task['workspace'], batch, snapshot['batches'][batch]['sha256'])).fetchone()[0]
                try:
                    validate_round(db, dict(idempotency_key='repair', scenario_file_sha256=snapshot['batches'][batch]['sha256'],
                        product=dict(commit=info['commit'], digest=info['product_digest'], dirty=False), submitted_by=session, results=group))
                except APIError as error:
                    reason = '；'.join(filter(None, [reason, *(p['problem'] for p in error.problems)]))
                db.execute('INSERT INTO fix_validations(task,attempt,validation,batch,version,repositories_json,product_digest,session_id,status,reason,results_json,evidence_json,skill_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task['id'], number, sequence, batch, version, canonical(info['repos']), info['product_digest'], session,
                     'fail' if reason else 'pass', reason, canonical(group), canonical(evidence), snapshot['skill_digest']))
            if reason:
                db.execute("UPDATE fix_validations SET status='fail',reason=? WHERE task=? AND attempt=? AND validation=?",
                           (reason, task['id'], number, sequence))
            result = dict(status='fail' if reason else 'pass', reason=reason, evidence=evidence,
                    session_id=session, skill_digest=snapshot['skill_digest'], repositories=info['repos'], scenarios=[dict(batch=s['batch'], scenario=s['id']) for s in selected])
            self.tasks.kernel.register(task, result, connection=db, phase=phase, number=number)
            return result

    def selection(self, task, problems):
        workspace, snapshot = self.fixes.service.workspace, task['snapshot']
        changed = {r['alias'] for r in task['repositories'] if r['changed_files']}
        required = {(p['batch'], p['scenario']) for p in problems}
        selected, aliases = [], set(snapshot['aliases'])
        for batch, version in snapshot['batches'].items():
            document = yaml.safe_load(version['content'])
            involved = workspace.aliases(document)
            for node in document['sops']:
                for scene in node['scenarios']:
                    if changed.intersection(involved) or (batch, scene['id']) in required:
                        selected.append(dict(scene, batch=batch))
                        aliases.update(involved)
        if not required.issubset({(s['batch'], s['id']) for s in selected}):
            raise ValueError('问题场景已不在固定快照中')
        return selected, sorted(aliases)

    def run(self, task, problems):
        workspace = self.fixes.service.workspace
        snapshot = task['snapshot']
        stale = self.fixes.snapshot_changes(task)
        if stale:
            return dict(status='void', reason=stale, evidence=[], scenarios=[])
        try:
            from .fixtures import definition
            snapshot = dict(snapshot, skill_digest=workspace.skill_digest(),
                            execution_entries=workspace.execution_entries(), fixtures=definition(workspace))
            self.tasks.worktree_environments[task['id']] = self.fixes.repositories.ensure(task['id'], snapshot['aliases'])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return dict(status='fail', reason='复测前工作树核对失败：' + str(error), evidence=[], scenarios=[])
        number = task['attempt']
        with self.store.connection() as db:
            sequence = db.execute('SELECT COALESCE(MAX(validation),0)+1 FROM fix_validations WHERE task=? AND attempt=?', (task['id'], number)).fetchone()[0]
        parent = self.tasks.directory(task['id']) / 'validations' / str(number)
        # 中断的复验可能还没有数据库记录，仍须保留其目录、日志和证据。
        while True:
            directory = parent / str(sequence)
            try:
                directory.mkdir(parents=True)
                break
            except FileExistsError:
                sequence += 1
        (directory / 'evidence').mkdir()
        directories = self.fixes.directories(task)
        try:
            selected, aliases = self.selection(task, problems)
        except ValueError as error:
            # 问题场景已不在固定快照中：输入变化，不是复测未通过（SOP-BOARD-5）。
            return dict(status='void', reason=str(error), evidence=[], scenarios=[])
        rows, session, reason, changed = [], '', '', ''
        process = None
        info = workspace.product(aliases, directories)
        try:
            moved = self.commit_change(task)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            moved = '复验前无法核对修复提交：' + str(error)
        if info['dirty'] or moved:
            # 修复提交后产品又被改动：复验依据已不是这次修复，记为作废，不计入连续未通过。
            return dict(status='void', reason=moved or '修复提交后复测目录出现未提交的产品改动', evidence=[], scenarios=[])
        try:
            preflight = self.fixes.service.environment.agent_environment(directories, self.tasks.worktree_environments[task['id']])
            blocked = blocked_results(selected, workspace.entries, preflight)
            blocked_ids = {(row['batch'], row['scenario']) for row in blocked}
            runnable = [scene for scene in selected if (scene['batch'], scene['id']) not in blocked_ids]
            if any(snapshot['entries']['entries'][s['verify']]['surface'] == 'web' for s in runnable):
                if not self.tasks.kernel.wait_for(task, self.tasks.validation_type.waiting,
                        lambda: self.tasks.kernel.browser(task, True, reason='等待验收浏览器'), lambda: self.active(task)):
                    return
            context = self.fixes.service.environment.use_repair(directories, self.tasks.worktree_environments[task['id']]) if runnable else nullcontext(preflight)
            with context as environment:
                if not self.active(task):
                    return
                runtime = dict(directory=directory, snapshot=snapshot, runnable=runnable, blocked=blocked,
                    info=info, selected=selected, aliases=aliases, directories=directories, number=number, sequence=sequence, rows=rows)
                prompt = self.tasks.validation_type.prepare(task, runtime=runtime, environment=environment, validator=self)
                if workspace.skill_changed(snapshot):
                    return dict(status='void', reason='验证技能变化', evidence=[], scenarios=[])
                if runnable:
                    with self.tasks.lock:
                        process = self.tasks.kernel.launch(task, directory, prompt, self.tasks.validation_type.launch(task, environment=environment))
                        self.tasks.processes[task['id']] = process
                    code = wait_codex(process, self.tasks.timeout, timeout_message='复测会话超时')
                    if code:
                        raise ValueError(codex_failure(directory, code, label='复测会话'))
                    session = session_id(directory, '')
                    if not session or session == task['session_id']:
                        raise ValueError('复测没有独立的新会话编号')
                else:
                    session = 'Flow Board 入口检查'
                reason = self.tasks.validation_type.consume(task, runtime=runtime, session=session, validator=self, register=False)
        except InputChanged as error:
            changed = str(error)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, APIError) as error:
            reason = '；'.join(p['problem'] for p in error.problems) if isinstance(error, APIError) else str(error)
        finally:
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
        if not self.active(task):
            return
        if changed:
            return dict(status='void', reason=changed, evidence=[], scenarios=[])
        # 会话失败、结果缺失等普通异常也可能与输入变化同时出现；登记事务内统一复核，输入变化优先记为作废。
        runtime = dict(directory=directory, snapshot=snapshot, info=info, rows=rows,
            selected=selected, number=number, sequence=sequence, aliases=aliases, directories=directories)
        return self.tasks.validation_type.consume(task, runtime=runtime, session=session, reason=reason, validator=self)

    def active(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == S.VERIFYING
