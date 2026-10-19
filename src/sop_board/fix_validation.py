"""修复候选沿用执行与检查运行器，结果在合入前独立保存。"""
import json
from importlib.resources import files
from pathlib import Path
from string import Template
import subprocess

import yaml

from .candidates import Candidates
from .checks import Checks
from .errors import APIError
from .executions import Executions
from .repositories import git, product
from .service import canonical, fingerprint
from .tasks import judgment_object, invalid_judgment_constant, terminate_group
from .validation import validate_round


class ValidationContext:
    """为共用运行器提供本次验证目录，生命周期仍由原修复任务管理。"""
    def __init__(self, tasks, directory):
        self.tasks, self.path = tasks, directory

    def __getattr__(self, name):
        return getattr(self.tasks, name)

    def directory(self, task_id):
        return self.path

    def read(self, task_id):
        task = self.tasks.read(task_id)
        return dict(task, status='运行中' if task['status'] == '复测中' else task['status'])


class FixValidation:
    def __init__(self, fixes):
        self.fixes, self.tasks, self.store = fixes, fixes.tasks, fixes.store

    def run(self, task, problems):
        number = task['attempt']
        with self.store.connection() as db:
            sequence = db.execute('SELECT COALESCE(MAX(validation),0)+1 FROM fix_candidates WHERE task=? AND attempt=?',
                (task['id'], number)).fetchone()[0]
        directory = self.tasks.directory(task['id']) / 'validations' / str(number) / str(sequence)
        directory.mkdir(parents=True)
        (directory / 'evidence').mkdir()
        context = ValidationContext(self.tasks, directory)
        candidates = Candidates(context)
        worktree = Path(task['worktree'])
        snapshot = task['snapshot']
        declarations = snapshot['entries']
        selected = []
        for problem in problems:
            batch = problem['batch']
            document = yaml.safe_load(snapshot['batches'][batch]['content'])
            scene = next(s for node in document['sops'] for s in node['scenarios'] if s['id'] == problem['scenario'])
            selected.append(dict(scene, batch=batch))
        if any(declarations['entries'][s['verify']]['candidate'] == 'in-place' for s in selected):
            return dict(status='pending', reason='原地替换由 T3 接入', evidence=[],
                        scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in problems])
        commit = git(worktree, 'rev-parse', 'HEAD').decode().strip()
        info = product(worktree, worktree / 'docs/sop', worktree / snapshot['entry_path'])
        rows, session, reason = [], '', ''
        guarded = [dict(scene, batch=batch) for batch, data in snapshot['batches'].items()
            for node in yaml.safe_load(data['content'])['sops'] for scene in node['scenarios']
            if (scene.get('regression') or {}).get('check')]
        check_rows = []
        process = None
        try:
            if any(declarations['entries'][s['verify']]['surface'] == 'web' for s in selected):
                while self.active(task):
                    with self.tasks.lock:
                        if self.tasks.browser_owner in (None, task['id']):
                            self.tasks.browser_owner = task['id']
                            self.tasks.update(task['id'], error='复测产品问题并运行回归检查')
                            break
                        self.tasks.update(task['id'], error='等待测试浏览器')
                    self.tasks.stopping.wait(.05)
                if not self.active(task):
                    return
            prepared = candidates.prepare(task, selected, declarations, worktree)
            if prepared is None:
                return
            scenes, failed, environment, targets = prepared
            for row in failed:
                # 同一场景编号可以属于多个批次；入口失败按原顺序归属。
                matches = [s for s in selected if s['id'] == row['scenario'] and not any(
                    r['batch'] == s['batch'] and r['scenario'] == s['id'] for r in rows)]
                row['batch'] = matches[0]['batch']
                row['fingerprint'] = fingerprint({k:v for k,v in matches[0].items() if k != 'batch'})
                rows.append(row)
            (directory / 'retest.json').write_text(canonical([dict(batch=s['batch'], scenario=s['id'], scene={k:v for k,v in s.items() if k != 'batch'}) for s in scenes]), encoding='utf-8')
            (directory / 'entries.json').write_text(canonical(dict(declarations, targets=targets)), encoding='utf-8')
            (directory / 'snapshot.json').write_text(canonical(dict(snapshot, candidate_commit=commit)), encoding='utf-8')
            (directory / 'cases.json').write_text(canonical(snapshot['cases']), encoding='utf-8')
            prompt = Template(files('sop_board').joinpath('prompts/fix-verify.md').read_text(encoding='utf-8')).substitute(
                task_directory=directory, candidate_directory=worktree)
            (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
            if scenes:
                with self.tasks.lock:
                    if not self.active(task):
                        return
                    process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', environment=environment)
                    self.tasks.processes[task['id']] = process
                try:
                    code = process.wait(timeout=self.tasks.timeout)
                except subprocess.TimeoutExpired:
                    raise ValueError(f'复测会话超时（{self.tasks.timeout:g} 秒）')
                if code:
                    raise ValueError(f'复测会话退出码 {code}')
                for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                        session = event['thread_id']
                        break
                if not session or session == task['session_id']:
                    raise ValueError('复测未返回独立的新会话编号')
                # 复用执行结果的证据路径、重复行、遗漏行与字段核对。
                executions = Executions(context)
                for batch in dict.fromkeys(s['batch'] for s in scenes):
                    group = [s for s in scenes if s['batch'] == batch]
                    bound = dict(task, snapshot_json=canonical(dict(scenarios={s['id']: fingerprint({k:v for k,v in s.items() if k != 'batch'}) for s in group})))
                    rows.extend(dict(row, batch=batch) for row in executions.read_results(bound, group, session, batch=batch))
                raw = [json.loads(line, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
                    for line in (directory / 'results.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
                allowed = {(s['batch'], s['id']) for s in scenes}
                if any((r.get('batch'), r.get('scenario')) not in allowed for r in raw):
                    raise ValueError('复测结果含范围外批次或场景')
            bad = [r for r in rows if r['status'] != 'pass']
            reason = '；'.join(r['actual'] for r in bad)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError, APIError) as error:
            reason = '；'.join(p['problem'] for p in error.problems) if isinstance(error, APIError) else str(error)
        finally:
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
            candidates.close(task['id'])
        if not self.active(task):
            return
        collected = Checks(context).collect(task, worktree, guarded, declarations)
        if collected is None:
            return
        for name, results, paths in collected:
            for row in results:
                row['evidence'] = paths
                row['regression_check'] = name
                check_rows.append(row)
        failed_checks = [r['actual'] for r in check_rows if r['status'] != 'pass']
        reason = '；'.join(filter(None, [reason, *failed_checks]))
        # 同一场景复测与守护都通过才通过；每批每场景只保留一条用于提升。
        combined = {(r['batch'], r['scenario']): r for r in rows}
        for row in check_rows:
            key = row['batch'], row['scenario']
            prior = combined.get(key)
            if prior is None:
                combined[key] = row
            else:
                if row['status'] != 'pass':
                    prior.update(status=row['status'], actual=row['actual'], **({'finding': row['finding']} if 'finding' in row else {}))
                prior['evidence'].extend(row['evidence'])
                prior['regression_check'] = row['regression_check']
        rows = list(combined.values())
        evidence = [str(p) for p in sorted(directory.rglob('*')) if p.is_file() and not p.is_symlink()]
        with self.store.transaction() as db:
            for row in rows:
                row['evidence'] = [dict(name=p.name, sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
            groups = []
            for batch in dict.fromkeys([p['batch'] for p in problems] + [s['batch'] for s in guarded]):
                results = [r for r in rows if r['batch'] == batch]
                version = db.execute('SELECT id FROM versions WHERE batch=(SELECT id FROM batches WHERE project=? AND name=?) AND sha256=? ORDER BY id DESC LIMIT 1',
                    (task['project'], batch, snapshot['batches'][batch]['sha256'])).fetchone()[0]
                if results:
                    try:
                        validate_round(db, dict(idempotency_key='candidate', scenario_file_sha256=snapshot['batches'][batch]['sha256'],
                            product=dict(commit=commit, digest=info['product_digest'], dirty=False), submitted_by=session or '候选启动', results=results))
                    except APIError as error:
                        reason = '；'.join(filter(None, [reason, *(p['problem'] for p in error.problems)]))
                groups.append((batch, version, results))
            status = 'fail' if reason else 'pass'
            for batch, version, results in groups:
                db.execute('INSERT INTO fix_candidates(task,attempt,validation,batch,version,commit_id,product_digest,session_id,status,reason,results_json,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task['id'], number, sequence, batch, version, commit, info['product_digest'], session, status, reason, canonical(results), canonical(evidence)))
        return dict(status=status, reason=reason, evidence=evidence, candidate_commit=commit, session_id=session,
                    checks=[dict(name=name, status='pass' if all(r['status'] == 'pass' for r in results) else 'fail') for name, results, _ in collected],
                    scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in problems])

    def active(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '复测中'
