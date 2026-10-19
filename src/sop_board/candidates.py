"""候选服务的启动、HTTP 就绪核对和按入口隔离的失败结果。"""
import hashlib
import os
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

from .service import now
from .tasks import codex_environment, terminate_group


class DirectResponse(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # 已收到重定向响应便满足就绪条件，无需访问另一个地址。
        return None


class Candidates:
    def __init__(self, tasks):
        self.tasks = tasks

    def active(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '运行中'

    def start(self, task, name, entry, worktree):
        directory = self.tasks.directory(task['id']) / 'evidence' / ('start-' + hashlib.sha256(name.encode()).hexdigest()[:12])
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / 'start.log'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        address = f'http://127.0.0.1:{port}'
        environment = dict(codex_environment(), SOP_PORT=str(port))
        if entry.get('address_env'):
            environment[entry['address_env']] = address
        process = None
        try:
            with log.open('wb') as stream, self.tasks.lock:
                if not self.active(task):
                    return None
                process = subprocess.Popen(entry['start'], cwd=worktree, env=environment,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                self.tasks.candidate_processes.setdefault(task['id'], []).append(process)
            deadline = time.monotonic() + 120
            opener = build_opener(ProxyHandler({}), DirectResponse())
            while self.active(task):
                code = process.poll()
                if code is not None:
                    lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
                    return address, f"退出码 {code}；{lines[-1] if lines else '无输出'}", log
                if time.monotonic() >= deadline:
                    terminate_group(process)
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write('候选版本启动失败：就绪超时（120 秒）\n')
                    return address, '就绪超时（120 秒）', log
                try:
                    with opener.open(address, timeout=min(.5, max(.01, deadline-time.monotonic()))):
                        return address, '', log
                except HTTPError:
                    return address, '', log
                except (OSError, URLError):
                    self.tasks.stopping.wait(.05)
            return None
        except OSError as error:
            log.write_text(f'启动命令无法运行：{error}', encoding='utf-8')
            return address, f'启动命令无法运行：{error}', log

    def prepare(self, task, scenes, declarations, worktree):
        environment = codex_environment()
        selected, results, targets = [], [], {}
        grouped = {}
        for scene in scenes:
            grouped.setdefault(scene['verify'], []).append(scene)
        for name, group in grouped.items():
            entry = declarations['entries'][name]
            variable = entry.get('address_env')
            kind = entry['candidate']
            status = finding = ''
            evidence = []
            targets[name] = dict(directory=str(worktree) if kind != 'in-place' else None)
            if kind == 'start':
                run = self.start(task, name, entry, worktree)
                if run is None:
                    return None
                address, problem, log = run
                targets[name]['address'] = address
                if problem:
                    status, finding, evidence = 'fail', '候选版本启动失败：' + problem, [log]
                elif variable:
                    environment[variable] = address
            elif kind == 'in-place' and variable:
                if os.environ.get(variable):
                    environment[variable] = os.environ[variable]
                    targets[name]['address_env'] = variable
                else:
                    status, finding = 'blocked', '看板环境缺少 ' + variable
            for scene in group:
                if status:
                    row = dict(scenario=scene['id'], status=status, actual=finding, evidence=evidence,
                        executed_at=now(), executor='候选启动 ' + name if kind == 'start' else '入口准备 ' + name,
                        session=str(task['id']))
                    if status == 'blocked':
                        row['finding'] = finding
                    results.append(row)
                else:
                    selected.append(scene)
        return selected, results, environment, targets

    def close(self, task_id):
        with self.tasks.lock:
            for process in self.tasks.candidate_processes.pop(task_id, []):
                terminate_group(process)
