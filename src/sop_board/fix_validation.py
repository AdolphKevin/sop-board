"""在各仓修复目录启动适配，由新会话复测问题与所有可能受影响场景。"""
import json
from importlib.resources import files
from pathlib import Path
from string import Template
import subprocess

import yaml

from .errors import APIError
from .executions import Executions
from .service import canonical, fingerprint
from .tasks import terminate_group, codex_failure
from .validation import validate_round
from .playwright import driver_skill, write_scope


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

    def run(self, task, problems):
        workspace = self.fixes.service.workspace
        snapshot = task['snapshot']
        stale = self.fixes.snapshot_changes(task)
        if stale:
            return dict(status='fail', reason=stale, evidence=[], scenarios=[])
        try:
            self.tasks.worktree_environments[task['id']] = self.fixes.repositories.ensure(task['id'], snapshot['aliases'])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return dict(status='fail', reason='复测前工作树核对失败：' + str(error), evidence=[], scenarios=[])
        number = task['attempt']
        with self.store.connection() as db:
            sequence = db.execute('SELECT COALESCE(MAX(validation),0)+1 FROM fix_validations WHERE task=? AND attempt=?', (task['id'], number)).fetchone()[0]
        directory = self.tasks.directory(task['id']) / 'validations' / str(number) / str(sequence)
        directory.mkdir(parents=True)
        (directory / 'evidence').mkdir()
        directories = self.fixes.directories(task)
        changed = {r['alias'] for r in task['repositories'] if r['changed_files']}
        required = {(p['batch'], p['scenario']) for p in problems}
        selected, aliases = [], set()
        # 保守覆盖所有声明涉及改动仓库的场景，避免漏掉跨批次依赖。
        for batch, version in snapshot['batches'].items():
            document = yaml.safe_load(version['content'])
            involved = workspace.aliases(document)
            affected = bool(changed.intersection(involved))
            for node in document['sops']:
                for scene in node['scenarios']:
                    if affected or (batch, scene['id']) in required:
                        selected.append(dict(scene, batch=batch))
                        aliases.update(involved)
        if not required.issubset({(s['batch'], s['id']) for s in selected}):
            return dict(status='fail', reason='问题场景已不在固定快照中', evidence=[], scenarios=[])
        aliases.update(snapshot['aliases'])
        aliases = sorted(aliases)
        rows, session, reason = [], '', ''
        process = None
        info = workspace.product(aliases, directories)
        if info['dirty']:
            return dict(status='fail', reason='复测目录有未提交的产品改动', evidence=[], scenarios=[])
        try:
            if any(snapshot['entries']['entries'][s['verify']]['surface'] == 'web' for s in selected):
                while self.active(task):
                    with self.tasks.lock:
                        if self.tasks.browser_owner in (None, task['id']):
                            self.tasks.browser_owner = task['id']
                            break
                        self.tasks.update(task['id'], error='等待验收浏览器')
                    self.tasks.stopping.wait(.1)
            with self.fixes.service.environment.use_repair(directories, self.tasks.worktree_environments[task['id']]) as environment:
                if not self.active(task):
                    return
                addresses = {name: environment[entry['address_env']] for name, entry in workspace.entries.items() if entry.get('address_env') and entry['address_env'] in environment}
                (directory / 'retest.json').write_text(canonical([dict(batch=s['batch'], scenario=s['id'], scene={k:v for k,v in s.items() if k != 'batch'}) for s in selected]), encoding='utf-8')
                (directory / 'entries.json').write_text(canonical(dict(entries=workspace.entries, addresses=addresses)), encoding='utf-8')
                (directory / 'snapshot.json').write_text(canonical(dict(snapshot, repositories=info['repos'])), encoding='utf-8')
                (directory / 'cases.json').write_text(canonical(snapshot['cases']), encoding='utf-8')
                web_scopes = []
                for index, batch in enumerate(dict.fromkeys(scene['batch'] for scene in selected), 1):
                    batch_scenes = [scene for scene in selected if scene['batch'] == batch and workspace.entries[scene['verify']]['surface'] == 'web' and workspace.entries[scene['verify']].get('driver') in (None, 'playwright-acceptance')]
                    if not batch_scenes:
                        continue
                    folder = directory / 'playwright' / str(index)
                    folder.mkdir(parents=True)
                    document = yaml.safe_load(snapshot['batches'][batch]['content'])
                    ids = {scene['id'] for scene in batch_scenes}
                    document['sops'] = [dict(node, scenarios=[scene for scene in node['scenarios'] if scene['id'] in ids])
                        for node in document['sops'] if any(scene['id'] in ids for scene in node['scenarios'])]
                    source = folder / 'scenarios.yaml'
                    source.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
                    scope_file = write_scope(folder, task['workspace'], batch, source, batch_scenes)
                    web_scopes.append(dict(batch=batch, scope=str(scope_file), scenarios=str(source)))
                (directory / 'playwright-scopes.json').write_text(canonical(web_scopes), encoding='utf-8')
                prompt = Template(files('sop_board').joinpath('prompts/fix-verify.md').read_text(encoding='utf-8')).substitute(task_directory=directory, playwright_skill=driver_skill() / 'SKILL.md')
                (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
                with self.tasks.lock:
                    process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', environment=environment)
                    self.tasks.processes[task['id']] = process
                try:
                    code = process.wait(timeout=self.tasks.timeout)
                except subprocess.TimeoutExpired:
                    terminate_group(process)
                    raise ValueError('复测会话超时')
                if code:
                    raise ValueError(codex_failure(directory, code, label='复测会话'))
                for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                        session = event['thread_id']
                        break
                if not session or session == task['session_id']:
                    raise ValueError('复测没有独立的新会话编号')
                executions = Executions(ValidationContext(self.tasks, directory))
                for batch in dict.fromkeys(s['batch'] for s in selected):
                    group = [s for s in selected if s['batch'] == batch]
                    bound = dict(task, input_json='{}', scope_json='[]', snapshot_json=canonical(dict(scenarios={s['id']: fingerprint(s) for s in group})))
                    rows.extend(executions.read_results(bound, group, session, batch=batch))
                raw = [json.loads(line) for line in (directory / 'results.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
                if any((r.get('batch'), r.get('scenario')) not in {(s['batch'], s['id']) for s in selected} for r in raw):
                    raise ValueError('复测结果有范围外批次或场景')
                workspace.reload()
                stale = self.fixes.snapshot_changes(task)
                if stale or workspace.product(aliases, directories) != info:
                    raise ValueError(stale or '复测期间仓库代码变化')
                failed = [r for r in rows if r.get('status') != 'pass']
                reason = '；'.join(str(r.get('actual', '')) for r in failed) or ('存在未通过场景' if failed else '')
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
        evidence = [str(p) for p in directory.rglob('*') if p.is_file() and not p.is_symlink()]
        with self.tasks.lock, self.store.transaction() as db:
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
                db.execute('INSERT INTO fix_validations(task,attempt,validation,batch,version,repositories_json,product_digest,session_id,status,reason,results_json,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task['id'], number, sequence, batch, version, canonical(info['repos']), info['product_digest'], session,
                     'fail' if reason else 'pass', reason, canonical(group), canonical(evidence)))
            if reason:
                db.execute("UPDATE fix_validations SET status='fail',reason=? WHERE task=? AND attempt=? AND validation=?",
                           (reason, task['id'], number, sequence))
        return dict(status='fail' if reason else 'pass', reason=reason, evidence=evidence,
                    session_id=session, repositories=info['repos'], scenarios=[dict(batch=s['batch'], scenario=s['id']) for s in selected])

    def active(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '复测中'
