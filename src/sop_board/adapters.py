"""适配进程、自检和同一用户本机唯一环境；不保存工作树适配返回的环境值。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import errno
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import threading
import time
from uuid import uuid4

from .repositories import git
from .worktrees import Worktrees, WorktreeError, is_board_input


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def stop_process(process, grace=10):
    # The foreground adapter owns its children and waits for them on SIGTERM.
    # Wait for our child directly; probing a dying process group with signal 0
    # can be denied by the host sandbox even after SIGTERM was delivered.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    except PermissionError:
        if process.poll() is None:
            raise
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def port_owners(ports):
    occupied = []
    for port in ports:
        busy = False
        for family, address in [(socket.AF_INET, ('0.0.0.0', port))] + ([(socket.AF_INET6, ('::', port))] if socket.has_ipv6 else []):
            try:
                with socket.socket(family) as sock:
                    if family == socket.AF_INET6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(address)
            except OSError as error:
                if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    continue
                busy = True
                break
        if busy:
            try:
                executable = shutil.which('lsof') or '/usr/sbin/lsof'
                result = subprocess.run([executable, '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                        capture_output=True, text=True, timeout=5)
                owner = result.stdout.strip() or '未取得进程信息'
            except (OSError, subprocess.SubprocessError):
                owner = '无法读取进程信息（需要 lsof）'
            occupied.append(dict(port=port, process=owner))
    return occupied


class Environment:
    def __init__(self, workspace):
        self.workspace = workspace
        self.worktrees = Worktrees(self)
        self.operation = threading.RLock()
        self.process = None
        self.reader = None
        self.log = None
        self.machine_lock = None
        self.state = dict(status='未启动', repositories={}, config_digest='', error='')
        self.extra = {}
        self.stopping = False
        self.check_thread = None
        self.check_request_lock = threading.Lock()

    def latest_check(self):
        files = sorted((self.workspace.data / 'checks').glob('*.json'))
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def approved(self):
        record = self.latest_check()
        return bool(self.workspace.configured and record and record['ok'] and
                    record['config_digest'] == self.workspace.config_digest)

    def environment(self, directories, extra=None, log_dir=None):
        env = {k: v for k, v in os.environ.items() if not is_board_input(k)}
        # Explicit repository inputs always override values inherited by the board.
        env.update(extra or {})
        for alias, path in directories.items():
            suffix = alias.upper().replace('-', '_')
            env['SOP_REPO_' + suffix] = str(path)
            env['SOP_BASE_' + suffix] = str(self.workspace.repos[alias])
        env['SOP_ENV_LOG_DIR'] = str(log_dir or self.workspace.data / 'env')
        return env

    def agent_environment(self, directories, extra=None):
        from .tasks import codex_environment
        full = self.environment(directories, extra)
        env = dict(codex_environment(), **(extra or {}))
        for key, value in full.items():
            if key.startswith(('SOP_REPO_', 'SOP_BASE_')) or key == 'SOP_ENV_LOG_DIR':
                env[key] = value
        for entry in self.workspace.entries.values():
            key = entry.get('address_env')
            if key and key in full:
                env[key] = full[key]
        return env

    def command(self, name):
        argv = list(self.workspace.adapter[name])
        executable = self.workspace.data / argv[0]
        if executable.is_file():
            argv[0] = str(executable)
        return argv

    def run_command(self, name, environment, log, json_output=False):
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(self.command(name), cwd=self.workspace.data,
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        failure = ''
        try:
            deadline = time.monotonic() + self.workspace.adapter['ready_timeout']
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.stopping or time.monotonic() >= deadline:
                        failure = '看板正在退出' if self.stopping else '等待命令结束超时'
                        stop_process(process)
                        stdout, stderr = process.communicate(timeout=5)
                        break
        except BaseException:
            stop_process(process)
            raise
        lines = stdout.decode('utf-8', errors='replace').splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        # 最后一行可能含环境值，不进入日志、任务输出或自检报告。
        visible = lines[:-1] if json_output else lines
        log.write_text('\n'.join(visible) + '\n' + stderr.decode('utf-8', errors='replace'), encoding='utf-8')
        if failure or process.returncode:
            raise ValueError(f'adapter.{name} ' + (failure or f'退出码 {process.returncode}') + '；日志：' + str(log))
        return lines[-1] if json_output and lines else ''

    def _acquire_machine(self):
        path = Path('/tmp') / f'sop-board-environment-{os.getuid()}.lock'
        lock = path.open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('本机已有看板环境运行，请先停止另一套环境')
        self.machine_lock = lock

    def _record(self):
        path = self.workspace.data / 'environment.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def _start(self, directories, extra=None):
        if self.process:
            self._stop()
        self._acquire_machine()
        directory = self.workspace.data / 'env'
        directory.mkdir(exist_ok=True)
        sequence = max((int(p.stem) for p in directory.glob('*.log') if p.stem.isdigit()), default=0) + 1
        self.log = directory / f'{sequence}.log'
        ready = threading.Event()
        self.extra = extra or {}
        try:
            repositories = {alias: dict(directory=str(path), commit=git(path, 'rev-parse', '--verify', 'HEAD').decode().strip())
                            for alias, path in directories.items()}
            self.state = dict(status='启动中', repositories=repositories, config_digest=self.workspace.config_digest,
                              log=str(self.log), started_at=stamp(), error='')
            self._record()
            self.process = subprocess.Popen(self.command('env'), cwd=self.workspace.data,
                env=self.environment(directories, self.extra, directory), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.state['pid'] = self.process.pid
            process = self.process
            def read():
                with self.log.open('wb') as stream:
                    for line in iter(process.stdout.readline, b''):
                        stream.write(line)
                        stream.flush()
                        if line.rstrip(b'\r\n') == b'sop-board: ready':
                            ready.set()
            self.reader = threading.Thread(target=read, name='adapter-output', daemon=True)
            self.reader.start()
            deadline = time.monotonic() + self.workspace.adapter['ready_timeout']
            while not ready.wait(.05):
                if process.poll() is not None:
                    raise ValueError(f'adapter.env 就绪前退出（退出码 {process.returncode}）；日志：{self.log}')
                if self.stopping or time.monotonic() >= deadline:
                    raise ValueError('adapter.env 等待就绪超时或看板正在退出；日志：' + str(self.log))
            if process.poll() is not None:
                raise ValueError('adapter.env 就绪后退出；日志：' + str(self.log))
            self.state['status'] = '就绪'
            self._record()
        except BaseException as error:
            self._stop()
            self.state.update(status='失败', error=str(error))
            self._record()
            raise

    def _stop(self):
        if self.process:
            stop_process(self.process)
            if self.reader:
                self.reader.join(timeout=5)
            self.process.stdout.close()
            self.process = None
        self.extra = {}
        if self.machine_lock:
            fcntl.flock(self.machine_lock, fcntl.LOCK_UN)
            self.machine_lock.close()
            self.machine_lock = None
        self.state['status'] = '已停止'
        self._record()

    def ensure_baseline(self):
        if self.stopping:
            raise ValueError('看板正在退出')
        if not self.approved():
            raise ValueError('尚未搭建或自检未通过，执行与修复停用')
        current = {a: dict(directory=str(p), commit=git(p, 'rev-parse', '--verify', 'HEAD').decode().strip())
                   for a, p in self.workspace.repos.items()}
        if (not self.process or self.process.poll() is not None or self.state['repositories'] != current
                or self.state['config_digest'] != self.workspace.config_digest):
            self._start(self.workspace.repos)

    @contextmanager
    def use_baseline(self):
        with self.operation:
            self.workspace.reload()
            self.ensure_baseline()
            yield self.agent_environment(self.workspace.repos)

    @contextmanager
    def use_repair(self, directories, extra):
        with self.operation:
            self.workspace.reload()
            if not self.approved():
                raise ValueError('工作区配置变化或自检未通过')
            self._stop()
            try:
                self._start(directories, extra)
                yield self.agent_environment(directories, extra)
            finally:
                self._stop()
                if not self.stopping and self.approved():
                    self.ensure_baseline()

    def restart(self):
        if not self.operation.acquire(blocking=False):
            raise ValueError('环境正在被执行、修复或自检使用')
        try:
            self.workspace.reload()
            if not self.approved():
                raise ValueError('配置变化或自检未通过，请先自检')
            self._stop()
            self.ensure_baseline()
        finally:
            self.operation.release()

    def reconcile(self):
        if not self.operation.acquire(blocking=False):
            return
        try:
            if self.process and not self.approved():
                self._stop()
        finally:
            self.operation.release()

    def public(self, local=False):
        state = dict(self.state)
        if self.process and self.process.poll() is not None:
            state['status'] = '已退出'
        record = self.latest_check()
        approved = self.approved()
        checking = bool(self.check_thread and self.check_thread.is_alive())
        instructions = [] if approved else [
            '工作区 ' + self.workspace.name + ' 尚未搭建完成，或配置变化后尚未自检。在以数据目录为工作目录的代理会话中，按 sop-board guide 继续搭建或修正下列问题：',
            *self.workspace.setup_instructions(),
            '搭建后运行 sop-board check ' + self.workspace.name + ' --data ' + str(self.workspace.data) + '；通过后启用执行与修复。']
        result = dict(status=state['status'], enabled=approved and not checking, checking=checking,
                      checked_at=record['finished_at'] if record else None, check_passed=approved,
                      setup_instructions=instructions,
                      ports=self.workspace.adapter['ports'] if self.workspace.configured else [])
        if local:
            lines = self.log.read_text(encoding='utf-8', errors='replace').splitlines()[-50:] if self.log and self.log.exists() else []
            result.update(state=state, latest_check=record, log_lines=lines, problems=self.workspace.problems)
        return result

    def check(self):
        if not self.operation.acquire(blocking=False):
            raise ValueError('环境正在被执行或修复使用，不能开始自检')
        try:
            if self.process:
                self._stop()
            self.workspace.reload()
            started = stamp()
            directory = self.workspace.data / 'checks'
            directory.mkdir(exist_ok=True)
            result = dict(started_at=started, config_digest=self.workspace.config_digest, ok=True, steps=[])
            def step(name, action):
                if not result['ok']:
                    result['steps'].append(dict(name=name, status='未执行', message='前一步未通过', log=None))
                    return
                try:
                    message, log = action()
                    result['steps'].append(dict(name=name, status='通过', message=message, log=log))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    result['ok'] = False
                    result['steps'].append(dict(name=name, status='失败', message=str(error), log=getattr(error, 'log', str(self.log) if self.log else None)))
            def config():
                problems = [p for p in self.workspace.problems if p not in self.workspace.skill_problems]
                if problems:
                    raise ValueError('；'.join(p['field'] + '：' + p['problem'] for p in problems))
                for path in self.workspace.repos.values():
                    git(path, 'rev-parse', '--verify', 'HEAD')
                return '配置和仓库路径合格', None
            def ports():
                busy = port_owners(self.workspace.adapter['ports'])
                if busy:
                    raise ValueError('端口被占用：\n' + '\n'.join(str(p['port']) + '\n' + p['process'] for p in busy))
                return '声明端口均空闲', None
            def baseline():
                try:
                    self._start(self.workspace.repos)
                    return '基线环境已就绪并停止', str(self.log)
                finally:
                    self._stop()
            def worktrees():
                identifier = uuid4().hex
                parent = self.workspace.data / 'worktrees' / ('check-' + identifier)
                branch = 'sop-board/check/' + identifier
                build_log, cleanup_log = directory / (started + '-worktree.log'), directory / (started + '-cleanup.log')
                paths, failure, established = {}, None, False
                try:
                    try:
                        paths, extra = self.worktrees.build(parent, branch, list(self.workspace.repos), build_log)
                    except WorktreeError as error:
                        paths = error.paths
                        raise
                    established = True
                    self._start(paths, extra)
                    log = str(self.log)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    failure = error
                finally:
                    try:
                        self._stop()
                        if paths:
                            self.worktrees.cleanup(parent, branch, paths, cleanup_log)
                        # 自检分支只在工作树已清理后删除；未核对的目录保留供检查。
                        for alias in paths:
                            if git(self.workspace.repos[alias], 'branch', '--list', branch).strip():
                                git(self.workspace.repos[alias], 'branch', '-d', branch)
                        registered = [p for root in self.workspace.repos.values() for p in self.worktrees.registered(root)]
                        if established and parent.exists() and not any(p.is_relative_to(parent) for p in registered):
                            shutil.rmtree(parent)
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        if failure:
                            raise WorktreeError(str(failure) + '；清理也失败：' + str(error), cleanup_log) from failure
                        raise
                if failure:
                    raise failure
                return '已建立并核对各仓工作树，环境就绪并清理；建立日志：' + str(build_log) + '；清理日志：' + str(cleanup_log), log
            def skills():
                self.workspace.reload()
                if self.workspace.skill_problems:
                    raise ValueError('；'.join(p['field'] + '：' + p['problem'] for p in self.workspace.skill_problems))
                used = ['验证技能：' + str(self.workspace.skill)]
                for name in self.workspace.entries:
                    helper = self.workspace.driver_paths.get(name)
                    used.append(name + ('；辅助技能：' + helper if helper else '；辅助技能：无'))
                return '；'.join(used), None
            for name, action in [('配置与仓库', config), ('端口', ports), ('基线环境', baseline), ('临时工作树环境', worktrees), ('验证技能', skills)]:
                step(name, action)
            if self.workspace.reload() or result['config_digest'] != self.workspace.config_digest:
                result['ok'] = False
                result['steps'][0].update(status='失败', message='自检期间配置变化，请重新自检')
            result['finished_at'] = stamp()
            path = directory / (started + '.json')
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
            return dict(result, path=str(path))
        finally:
            self.operation.release()

    def start_check(self):
        with self.check_request_lock:
            if self.check_thread and self.check_thread.is_alive():
                raise ValueError('自检正在运行')
            if not self.operation.acquire(blocking=False):
                raise ValueError('环境正在被执行或修复使用')
            self.operation.release()
            def run():
                try:
                    result = self.check()
                    if result['ok'] and not self.stopping:
                        with self.operation:
                            self.ensure_baseline()
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.state.update(status='失败', error=str(error))
            self.check_thread = threading.Thread(target=run, name='workspace-check', daemon=True)
            self.check_thread.start()

    def close(self):
        self.stopping = True
        if self.check_thread:
            self.check_thread.join()
        with self.operation:
            self._stop()
