"""将首仓修复工作树交给同一个适配环境，由新会话经真实入口复测。"""
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
        workspace.reload()
        if snapshot['config_digest'] != workspace.config_digest:
            return dict(status='fail', reason='工作区配置变化', evidence=[], scenarios=[])
        number = task['attempt']
        with self.store.connection() as db:
            sequence = db.execute('SELECT COALESCE(MAX(validation),0)+1 FROM fix_validations WHERE task=? AND attempt=?', (task['id'], number)).fetchone()[0]
        directory = self.tasks.directory(task['id']) / 'validations' / str(number) / str(sequence)
        directory.mkdir(parents=True)
        (directory / 'evidence').mkdir()
        directories = self.fixes.directories(task)
        selected = []
        for problem in problems:
            document = yaml.safe_load(snapshot['batches'][problem['batch']]['content'])
            scene = next(s for node in document['sops'] for s in node['scenarios'] if s['id'] == problem['scenario'])
            selected.append(dict(scene, batch=problem['batch']))
        rows, session, reason = [], '', ''
        process = None
        info = workspace.product(snapshot['aliases'], directories)
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
            with self.fixes.service.environment.use_repair(directories, workspace.data / 'prepare' / f"verify-{task['id']}-{number}-{sequence}.log") as environment:
                if not self.active(task):
                    return
                addresses = {name: environment[entry['address_env']] for name, entry in workspace.entries.items() if entry.get('address_env') and entry['address_env'] in environment}
                (directory / 'retest.json').write_text(canonical([dict(batch=s['batch'], scenario=s['id'], scene={k:v for k,v in s.items() if k != 'batch'}) for s in selected]), encoding='utf-8')
                (directory / 'entries.json').write_text(canonical(dict(entries=workspace.entries, addresses=addresses)), encoding='utf-8')
                (directory / 'snapshot.json').write_text(canonical(dict(snapshot, repositories=info['repos'])), encoding='utf-8')
                (directory / 'cases.json').write_text(canonical(snapshot['cases']), encoding='utf-8')
                prompt = Template(files('sop_board').joinpath('prompts/fix-verify.md').read_text(encoding='utf-8')).substitute(task_directory=directory)
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
                if workspace.config_digest != snapshot['config_digest'] or workspace.product(snapshot['aliases'], directories) != info:
                    raise ValueError('复测期间配置或代码变化')
                reason = '；'.join(r['actual'] for r in rows if r['status'] != 'pass')
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
            for batch in dict.fromkeys(p['batch'] for p in problems):
                group = [r for r in rows if r['batch'] == batch]
                version = db.execute('SELECT id FROM versions WHERE batch=(SELECT id FROM batches WHERE workspace=? AND name=?) AND sha256=? ORDER BY id DESC LIMIT 1',
                    (task['workspace'], batch, snapshot['batches'][batch]['sha256'])).fetchone()[0]
                try:
                    validate_round(db, dict(idempotency_key='repair', scenario_file_sha256=snapshot['batches'][batch]['sha256'],
                        product=dict(commit=info['commit'], digest=info['product_digest'], dirty=False), submitted_by=session, results=group))
                except APIError as error:
                    reason = '；'.join(filter(None, [reason, *(p['problem'] for p in error.problems)]))
                db.execute('INSERT INTO fix_validations(task,attempt,validation,batch,version,commit_id,product_digest,session_id,status,reason,results_json,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task['id'], number, sequence, batch, version, info['repos'][task['repo_alias']]['commit'], info['product_digest'], session,
                     'fail' if reason else 'pass', reason, canonical(group), canonical(evidence)))
        return dict(status='fail' if reason else 'pass', reason=reason, evidence=evidence,
                    session_id=session, repositories=info['repos'], scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in problems])

    def active(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '复测中'
