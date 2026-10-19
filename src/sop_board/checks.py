"""在固定 Git 快照中运行声明的检查，并把可追溯结果登记为 Agent 轮次。"""
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

import yaml

from .repositories import git, product, product_files
from .service import canonical, fingerprint, now
from .tasks import codex_environment, terminate_group
from .statistics import calculate


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def record_case(cases, name, status):
    # 同名报告可能来自重复运行；一次失败或跳过不能被后来的通过覆盖。
    priority = {'pass': 0, 'skip': 1, 'fail': 2}
    cases[name] = max((cases.get(name, 'pass'), status), key=priority.__getitem__)


class Checks:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.next_review = {}

    def cleanup_worktrees(self):
        directory = self.store.data.resolve() / 'worktrees'
        if directory.exists():
            for path in directory.iterdir():
                for root in self.service.writable_repos.values():
                    try:
                        git(root, 'worktree', 'remove', '--force', str(path))
                        break
                    except subprocess.CalledProcessError:
                        continue
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
        for root in self.service.writable_repos.values():
            git(root, 'worktree', 'prune')

    def freeze(self, task, selected=None):
        root = self.service.writable_repos[task['project']]
        declarations = self.service.entries(task['project'])
        entry_path = root / ('.tw/config.json' if declarations['source'] == 'tw-config' else 'docs/sop/entries.yaml')
        info = product(root, root / 'docs/sop' / (task['batch'] + '.yaml'), entry_path)
        with self.store.connection() as db:
            batch = self.service.batch(db, task['project'], task['batch'])
            version = self.service.latest_version(db, batch)
            document = yaml.safe_load(version['content'])
            cases = self.service.case_union(db, batch)
            guarded = {s['scenario'] for s in calculate(db, batch)['scenarios']
                       if (s['regression'] or {}).get('check') and s['latest_agent'] and
                       (not s['latest_manual'] or s['latest_agent']['number'] > s['latest_manual']['number']) and
                       s['latest_agent']['status'] == 'pass' and s['latest_agent']['product_digest'] != info['product_digest']}
        scope = {s['scenario'] for s in json.loads(task['scope_json'])}
        if selected is not None:
            scope = set(selected)
        elif task['kind'] == 'execute':
            scope |= guarded
        scenes = [s for n in document['sops'] for s in n['scenarios'] if s['id'] in scope]
        snapshot = dict(scenario_file_sha256=version['sha256'], scenarios={s['id']: fingerprint(s) for s in scenes},
                        entries_sha256=digest(dict(entries=declarations['entries'], checks=declarations['checks'])),
                        cases_sha256=digest(cases) if cases else '', commit=info['commit'], product_digest=info['product_digest'])
        return snapshot, scenes, declarations

    def prepare(self, task):
        if time.monotonic() < self.next_review.get(task['id'], 0):
            return False
        self.next_review[task['id']] = time.monotonic() + 5
        declarations = self.service.entries(task['project'])
        if not declarations['ok']:
            self.tasks.update(task['id'], status='排队中', error='入口声明不合格：' + '；'.join(p['problem'] for p in declarations['problems']))
            return False
        root = self.service.writable_repos[task['project']]
        entry = root / ('.tw/config.json' if declarations['source'] == 'tw-config' else 'docs/sop/entries.yaml')
        with self.store.connection() as db:
            batch = self.service.batch(db, task['project'], task['batch'])
        scenario = root / batch['path']
        current = {f['path']: f for f in product_files(root, scenario, entry)}
        head = {f['path']: f for f in product_files(root, scenario, entry, head=True)}
        changed = [path for path in sorted(current.keys() | head.keys()) if current.get(path) != head.get(path)]
        if changed:
            self.tasks.update(task['id'], status='待提交', error='工作区有未提交的产品改动：' + '、'.join(changed[:10]))
            return False
        snapshot, scenes, declarations = self.freeze(task)
        scope = {s['scenario'] for s in json.loads(task['scope_json'])}
        valid = {s['id'] for s in scenes if task['kind'] == 'execute' or (s.get('regression') or {}).get('check')}
        if not scope <= valid:
            raise ValueError('检查范围中的场景已移除或不再声明回归检查：' + '、'.join(sorted(scope - valid)))
        if snapshot['product_digest'] != digest(list(head.values())):
            self.tasks.update(task['id'], status='待提交', error='工作区有未提交的产品改动，快照固定时产品发生变化')
            return False
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'snapshot.json').write_text(canonical(snapshot), encoding='utf-8')
        task['snapshot_json'] = canonical(snapshot)
        with self.store.connection() as db:
            document = yaml.safe_load(self.service.latest_version(db, batch)['content'])
        task['input_json'] = canonical(dict(scenes=scenes, declarations=declarations,
            document=document,
            workspace_scenario_sha256=hashlib.sha256(scenario.read_bytes()).hexdigest() if scenario.is_file() else ''))
        self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'], error='')
        return True

    def changes(self, task, snapshot):
        current, _, declarations = self.freeze(task, selected=snapshot['scenarios'])
        labels = dict(product_digest='工作区产品指纹', commit='HEAD 提交', scenario_file_sha256='场景版本',
                      scenarios='所选场景内容指纹（' + '、'.join(sorted(s for s in snapshot['scenarios'].keys() | current['scenarios'].keys()
                          if snapshot['scenarios'].get(s) != current['scenarios'].get(s))) + '）',
                      entries_sha256='入口与检查声明', cases_sha256='用例并集')
        changed = [label for key, label in labels.items() if snapshot[key] != current[key]]
        if not declarations['ok'] and '入口与检查声明' not in changed:
            changed.append('入口与检查声明')
        with self.store.connection() as db:
            batch = self.service.batch(db, task['project'], task['batch'])
        source = self.service.writable_repos[task['project']] / batch['path']
        workspace_sha = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ''
        if workspace_sha != json.loads(task['input_json'])['workspace_scenario_sha256']:
            changed.append('工作区场景文件')
        return '、'.join(changed) + '变化' if changed else ''

    def run_check(self, task, worktree, name, check):
        output = self.tasks.directory(task['id']) / 'checks' / name
        output.mkdir(parents=True, exist_ok=True)
        cwd = worktree / check['cwd']
        argv = [arg.replace('{output}', str(output)) for arg in check['argv']]
        missing = [name for name in check['required_env'] if not os.environ.get(name)]
        if missing:
            problem = '受阻：缺少环境变量 ' + '、'.join(missing)
            (output / 'output.txt').write_text(problem + '\n', encoding='utf-8')
            return dict(cases={}, problem=problem, code=None, blocked=True, evidence=[output / 'output.txt'])
        environment = dict(codex_environment(), **{name: os.environ[name] for name in check['required_env']})
        problem = ''
        code = None
        stdout = b''
        process = None
        with (output / 'output.txt').open('wb') as stream:
            try:
                with self.tasks.lock:
                    if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                        return None
                    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=stream, start_new_session=True)
                    self.tasks.processes[task['id']] = process
                try:
                    stdout, _ = process.communicate(timeout=check['timeout'])
                except subprocess.TimeoutExpired:
                    terminate_group(process)
                    stdout, _ = process.communicate()
                    problem = f"回归检查 {name} 超时（{check['timeout']:g} 秒）"
                code = process.returncode
            except OSError as error:
                problem = f'回归检查 {name} 无法运行：{error}'
                stream.write((problem + '\n').encode())
            finally:
                if process:
                    terminate_group(process)
                with self.tasks.lock:
                    self.tasks.processes.pop(task['id'], None)
                stream.write(stdout)
        evidence = [output / 'output.txt']
        if problem:
            return dict(cases={}, problem=problem, code=code, evidence=evidence)
        if check['format'] == 'junit':
            report = Path(check['report'].replace('{output}', str(output)))
            if not report.is_absolute():
                report = cwd / report
        else:
            report = output / 'report.jsonl'
            report.write_bytes(stdout)
        cases = {}
        if not report.is_file() or (check['format'] == 'go-json' and not report.read_bytes().strip()):
            problem = f'回归检查 {name} 报告缺失'
        else:
            evidence.append(report)
            try:
                if check['format'] == 'junit':
                    xml = ET.parse(report).getroot()
                    if xml.tag not in ('testsuite', 'testsuites'):
                        raise ValueError('JUnit 根节点应为 testsuite 或 testsuites')
                    for case in xml.iter('testcase'):
                        record_case(cases, f"{case.get('classname', '')}::{case.get('name', '')}",
                            'fail' if case.find('failure') is not None or case.find('error') is not None else
                            'skip' if case.find('skipped') is not None else 'pass')
                else:
                    for line in report.read_text(encoding='utf-8').splitlines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError('Go JSON 事件应为对象')
                        if event.get('Test') and event.get('Action') in ('pass', 'fail', 'skip'):
                            record_case(cases, f"{event['Package']}::{event['Test']}", event['Action'])
            except (OSError, ValueError, KeyError, ET.ParseError) as error:
                problem = f'回归检查 {name} 报告无法解析：{error}'
        return dict(cases=cases, problem=problem, code=code, evidence=evidence)


    def collect(self, task, worktree, scenes, declarations):
        collected = []
        grouped = {}
        for scene in scenes:
            grouped.setdefault(scene['regression']['check'], []).append(scene)
        for name, selected in grouped.items():
            check = declarations['checks'][name]
            run = self.run_check(task, worktree, name, check)
            if run is None:
                return None
            cases = run['cases']
            results = []
            for scene in selected:
                declared = scene['regression']['cases']
                issues = []
                for state, label in [('missing', '缺失'), ('fail', '失败'), ('skip', '跳过')]:
                    names = [case for case in declared if cases.get(case, 'missing') == state]
                    if names:
                        issues.append(label + '：' + '、'.join(names))
                actual = run['problem'] or (f"回归检查 {name} 未通过：" + '；'.join(issues) if issues else
                          f"回归检查 {name} 通过：声明的 {len(declared)} 个用例全部通过")
                if (issues or run['code']) and not run['problem']:
                    actual += f"；命令退出码 {run['code']}"
                result = dict(scenario=scene['id'], fingerprint=fingerprint(scene),
                              status='blocked' if run.get('blocked') else 'fail' if issues or run['problem'] else 'pass',
                              actual=actual,
                              executed_at=now(), executor='回归检查 ' + name, session=str(task['id']), evidence=[])
                if run.get('blocked'):
                    result['finding'] = run['problem']
                results.append(result)
            collected.append((name, results, run['evidence']))
        return collected

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        root = self.service.writable_repos[task['project']]
        worktree = self.store.data.resolve() / 'worktrees' / str(task['id'])
        error = ''
        void_reason = ''
        rounds = []
        collected = []
        try:
            snapshot = json.loads(task['snapshot_json'])
            inputs = json.loads(task['input_json'])
            scenes, declarations = inputs['scenes'], inputs['declarations']
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(root, 'worktree', 'add', '--detach', str(worktree), snapshot['commit'])
            collected = self.collect(task, worktree, scenes, declarations)
            if collected is None:
                collected = []
                return
            with self.tasks.lock:
                void_reason = self.changes(task, snapshot)
                if void_reason:
                    return
            with self.tasks.lock, self.store.transaction() as db:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                    return
                batch = self.service.batch(db, task['project'], task['batch'])
                version = self.service.latest_version(db, batch)
                void_reason = self.changes(task, snapshot)
                if void_reason:
                    return
                for name, results, evidence in collected:
                    saved = [dict(name=p.name, sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in evidence]
                    for result in results:
                        result['evidence'] = saved
                    body = dict(product=dict(commit=snapshot['commit'], digest=snapshot['product_digest'], dirty=False),
                                submitted_by='回归检查 ' + name, results=results)
                    rounds.append(self.service.insert_round(db, batch, version, body, regression_check=name))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError, ET.ParseError) as problem:
            error = f'回归检查运行失败：{problem}'
        finally:
            process = self.tasks.processes.pop(task['id'], None)
            if process:
                terminate_group(process)
            if worktree.exists():
                try:
                    git(root, 'worktree', 'remove', '--force', str(worktree))
                except (OSError, subprocess.SubprocessError) as problem:
                    error = error or f'无法清理检查工作目录：{problem}'
            with self.tasks.lock:
                if not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '运行中':
                    result = dict(ok=not error, problems=[error] if error else [], checks=[
                        dict(name=name, status='fail' if any(r['status'] == 'fail' for r in results) else results[0]['status'])
                        for name, results, _ in collected] if not error else [])
                    (directory / 'check.json').write_text(canonical(result), encoding='utf-8')
                    self.tasks.update(task['id'], status='已作废' if void_reason else '失败' if error else '已完成', finished_at=now(),
                                      void_reason=void_reason, error=error, rounds_json=canonical(rounds), result_json=canonical(result))
