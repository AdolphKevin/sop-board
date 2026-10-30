"""适配进程、自检和按端口互斥的环境；不保存工作树适配返回的环境值。"""
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

from .repositories import git, git_identities
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
        # 开启地址复用后，macOS 上通配地址能与只监听回环地址的进程共存，所以回环地址也要单独探测。
        candidates = [(socket.AF_INET, ('0.0.0.0', port)), (socket.AF_INET, ('127.0.0.1', port))]
        if socket.has_ipv6:
            candidates += [(socket.AF_INET6, ('::', port)), (socket.AF_INET6, ('::1', port))]
        for family, address in candidates:
            try:
                with socket.socket(family) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(address)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EPERM):
                    raise
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


class PortBusy(ValueError):
    def __init__(self, port, owner):
        self.port, self.owner = port, owner
        super().__init__(f'端口 {port} 正由工作区 {owner} 的环境使用')


class EnvironmentUnhealthy(ValueError):
    """外部环境探测（含自愈后复测）未通过。"""


class CheckUnavailable(ValueError):
    """当前运行条件不足，不能据此否定已完成的配置自检。"""


def permission_failure(value):
    value = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or '')
    return next((message for message in ('operation not permitted','permission denied','read-only file system')
                 if message in value.casefold()), '')


def unavailable_reason(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,(CheckUnavailable,PermissionError,FileNotFoundError,subprocess.TimeoutExpired)):
            return str(error)
        if isinstance(error,OSError) and error.errno in (errno.EACCES,errno.EPERM,errno.EROFS,errno.ENOSPC):
            return str(error)
        if isinstance(error,subprocess.CalledProcessError):
            reason = permission_failure(error.stderr) or permission_failure(error.output)
            if reason:
                return '进程操作被当前环境拒绝：' + reason
        error = error.__cause__ or error.__context__
    return ''


class Environment:
    def __init__(self, workspace):
        self.workspace = workspace
        self.worktrees = Worktrees(self)
        self.operation = threading.RLock()
        self.process = None
        self.reader = None
        self.log = None
        self.port_locks = {}
        self.next_port_retry = 0
        self.state = dict(status='未启动', repositories={}, config_digest='', error='')
        self.extra = {}
        self.stopping = False
        self.check_thread = None
        self.check_request_lock = threading.Lock()
        # 外部环境探测未通过时的提醒（由 Service 接到待人处理队列）。
        self.alert = None

    def external(self):
        """adapter.mode: external（D97）：服务由使用者或其脚本启停，看板只探测与执行已授权的自愈命令。"""
        return self.workspace.adapter.get('mode') == 'external'

    def resolve(self, argv):
        argv = list(argv)
        executable = self.workspace.data / argv[0]
        if executable.is_file():
            argv[0] = str(executable)
        return argv

    def probe(self):
        """逐项只读探测；URL 以收到小于 500 的 HTTP 状态为通过，端口以能建立连接为通过，命令以退出码 0 为通过。
        不保存探测输出，避免环境值进入记录。"""
        import urllib.error
        import urllib.request
        results = []
        for item in self.workspace.adapter.get('health', []):
            timeout = item.get('timeout', 10)
            try:
                if 'url' in item:
                    try:
                        with urllib.request.urlopen(urllib.request.Request(item['url'], method='GET'), timeout=timeout) as response:
                            code = response.status
                    except urllib.error.HTTPError as error:
                        code = error.code
                    ok, message = code < 500, f'HTTP {code}'
                elif 'port' in item:
                    with socket.create_connection((item.get('host', '127.0.0.1'), item['port']), timeout=timeout):
                        pass
                    ok, message = True, '端口可连接'
                else:
                    code = subprocess.run(self.resolve(item['command']), cwd=self.workspace.data, env=self.environment(self.workspace.repos),
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          timeout=timeout).returncode
                    ok, message = code == 0, f'退出码 {code}'
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                ok, message = False, (str(getattr(error, 'reason', '') or error) or type(error).__name__)[:200]
            results.append(dict(name=item['name'], ok=ok, message=message))
        return results

    def heal(self):
        """依次执行已授权的自愈命令（如重连端口转发）。命令须在完成后退出，常驻进程由脚本自行放到后台；
        输出不保存，只记录退出码。返回失败说明。"""
        failures = []
        for index, argv in enumerate(self.workspace.adapter.get('heal', []), 1):
            try:
                code = subprocess.run(self.resolve(argv), cwd=self.workspace.data, env=self.environment(self.workspace.repos),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      timeout=self.workspace.adapter['ready_timeout'], start_new_session=True).returncode
                if code:
                    failures.append(f'自愈命令 {index} 退出码 {code}')
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(f'自愈命令 {index} 未能运行：{error}')
        return failures

    def check_external(self, *, alert=True, record=True):
        """探测；未通过且声明了自愈命令时先自愈再探测一次；仍未通过时提醒登记待人处理。返回是否通过。"""
        probes = self.probe()
        healed, failures = False, []
        if not all(p['ok'] for p in probes) and self.workspace.adapter.get('heal'):
            failures, healed = self.heal(), True
            probes = self.probe()
        ok = all(p['ok'] for p in probes)
        failed = [p['name'] for p in probes if not p['ok']]
        error = '' if ok else ('外部环境探测未通过：' + '、'.join(failed) +
                               ('（已执行自愈命令后仍未通过）' if healed else '（未声明自愈命令）'))
        self.state = dict(status='外部环境就绪' if ok else '外部环境异常', mode='external', probes=probes, healed=healed,
                          heal_failures=failures, probed_at=stamp(), repositories={}, config_digest=self.workspace.config_digest, error=error)
        if record:
            self._record()
        if not ok and alert and self.alert:
            self.alert(error)
        return ok

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

    def latest_check_attempt(self):
        directory = self.workspace.data / 'checks'
        paths = [*directory.glob('*.json'), *directory.glob('unavailable/*.json')]
        if not paths:
            return None
        path = max(paths,key=lambda p:p.name)
        try:
            return dict(json.loads(path.read_text(encoding='utf-8')),path=str(path))
        except (OSError,ValueError,TypeError):
            return None

    def save_check(self, result):
        result['finished_at'] = stamp()
        directory = self.workspace.data / 'checks'
        if not result['completed']:
            directory /= 'unavailable'
            result['message'] = ('自检未能在当前环境运行：' + result['reason'] +
                '。请在沙箱外按已授权范围运行 sop-board check；本次不替换已完成的自检结果。')
        path = directory / (result['started_at'] + '.json')
        temporary = path.with_suffix('.tmp')
        try:
            directory.mkdir(parents=True,exist_ok=True)
            temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(path)
        except OSError as error:
            # 报告目录不可写也不是配置失败；不尝试改动旧报告或另存到用户目录。
            return dict(result,ok=False,completed=False,path=None,reason=str(error),
                message='自检未能在当前环境运行：无法保存报告：'+str(error)+'。请在沙箱外核对数据目录权限后运行 sop-board check。')
        return dict(result,path=str(path))

    def environment(self, directories, extra=None, log_dir=None):
        env = {k: v for k, v in os.environ.items() if not is_board_input(k)}
        # Explicit repository inputs always override values inherited by the board.
        env.update(extra or {})
        from .credentials import names
        for key in names(self.workspace.credential_entries()):
            env.pop(key, None)
            if os.environ.get(key):
                env[key] = os.environ[key]
        for alias, path in directories.items():
            suffix = alias.upper().replace('-', '_')
            env['SOP_REPO_' + suffix] = str(path)
            env['SOP_BASE_' + suffix] = str(self.workspace.repos[alias])
        env['SOP_ENV_LOG_DIR'] = str(log_dir or self.workspace.data / 'env')
        return env

    def agent_environment(self, directories, extra=None, *, credential_names=()):
        from .tasks import codex_environment
        from .credentials import AgentEnvironment, names
        full = self.environment(directories, extra)
        env = dict(codex_environment(), **(extra or {}))
        for key, value in full.items():
            if key.startswith(('SOP_REPO_', 'SOP_BASE_')) or key == 'SOP_ENV_LOG_DIR':
                env[key] = value
        for entry in self.workspace.entries.values():
            key = entry.get('address_env')
            if key and key in full:
                env[key] = full[key]
        declared = sorted(set(names(self.workspace.credential_entries())) | set(credential_names))
        for key in declared:
            # 凭据只取启动看板的进程环境，不能被工作树适配返回值替换。
            env.pop(key, None)
            if os.environ.get(key):
                env[key] = os.environ[key]
        return AgentEnvironment(env, declared)

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
        from .credentials import names, redact
        secrets = [os.environ[name] for name in names(self.workspace.credential_entries()) if os.environ.get(name)]
        log.write_bytes(redact(('\n'.join(visible) + '\n' + stderr.decode('utf-8', errors='replace')).encode(), secrets))
        if failure or process.returncode:
            permission = permission_failure(stderr) or permission_failure(stdout)
            kind = CheckUnavailable if permission or failure == '看板正在退出' else ValueError
            raise kind(f'adapter.{name} ' + (failure or f'退出码 {process.returncode}') +
                ('；当前环境权限不足：' + permission if permission else '') + '；日志：' + str(log))
        return lines[-1] if json_output and lines else ''

    def _release_ports(self):
        for lock in self.port_locks.values():
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.port_locks.clear()

    def _acquire_ports(self):
        ports = sorted(self.workspace.adapter['ports'])
        if list(self.port_locks) == ports:
            return
        self._release_ports()
        try:
            for port in ports:
                path = Path('/tmp') / f'sop-board-port-{os.getuid()}-{port}.lock'
                lock = path.open('a+')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        lock.seek(0)
                        owner = json.load(lock).get('workspace') or '未知'
                    except (ValueError, AttributeError):
                        owner = '未知'
                    finally:
                        lock.close()
                    raise PortBusy(port, owner)
                except BaseException:
                    lock.close()
                    raise
                self.port_locks[port] = lock
                lock.seek(0)
                lock.truncate()
                json.dump(dict(workspace=self.workspace.name, data=str(self.workspace.data)), lock, ensure_ascii=False)
                lock.flush()
        except BaseException:
            self._release_ports()
            raise

    def check_addresses(self, extra=None):
        environment = self.environment(self.workspace.repos, extra)
        missing = [f'入口 {name} 缺少非空地址环境变量 {entry["address_env"]}'
                   for name, entry in self.workspace.entries.items()
                   if entry.get('address_env') and not environment.get(entry['address_env'], '').strip()]
        if missing:
            raise ValueError('；'.join(missing))

    def _record(self):
        path = self.workspace.data / 'environment.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def _start(self, directories, extra=None):
        if self.process:
            self._stop()
        try:
            self._acquire_ports()
        except PortBusy as error:
            self.state = dict(status=f'等待端口 {error.port}（工作区 {error.owner}）', repositories={},
                              config_digest=self.workspace.config_digest, error=str(error),
                              waiting_port=error.port, waiting_workspace=error.owner)
            self.next_port_retry = time.monotonic() + 10
            self._record()
            raise
        try:
            directory = self.workspace.data / 'env'
            directory.mkdir(exist_ok=True)
            sequence = max((int(p.stem) for p in directory.glob('*.log') if p.stem.isdigit()), default=0) + 1
            self.log = directory / f'{sequence}.log'
            ready = threading.Event()
            self.extra = extra or {}
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
            from .credentials import names, StreamRedactor
            secrets = [os.environ[name] for name in names(self.workspace.credential_entries()) if os.environ.get(name)]
            def read():
                redactor = StreamRedactor(secrets)
                with self.log.open('wb') as stream:
                    for line in iter(process.stdout.readline, b''):
                        stream.write(redactor.feed(line))
                        stream.flush()
                        if line.rstrip(b'\r\n') == b'sop-board: ready':
                            ready.set()
                    stream.write(redactor.feed(b'', final=True))
            self.reader = threading.Thread(target=read, name='adapter-output', daemon=True)
            self.reader.start()
            deadline = time.monotonic() + self.workspace.adapter['ready_timeout']
            while not ready.wait(.05):
                if process.poll() is not None:
                    self.reader.join(timeout=2)
                    permission = permission_failure(self.log.read_bytes())
                    if permission:
                        raise CheckUnavailable('adapter.env 被当前环境拒绝：' + permission + '；日志：' + str(self.log))
                    raise ValueError(f'adapter.env 就绪前退出（退出码 {process.returncode}）；日志：{self.log}')
                if self.stopping or time.monotonic() >= deadline:
                    kind = CheckUnavailable if self.stopping else ValueError
                    raise kind('adapter.env 等待就绪超时或看板正在退出；日志：' + str(self.log))
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
        self._release_ports()
        self.state.pop('waiting_port', None)
        self.state.pop('waiting_workspace', None)
        self.state['status'] = '已停止'
        self._record()

    def ensure_baseline(self):
        if self.stopping:
            raise ValueError('看板正在退出')
        if not self.approved():
            raise ValueError('尚未搭建或自检未通过，执行与修复停用')
        if self.external():
            if not self.check_external():
                raise EnvironmentUnhealthy(self.state['error'] + '；已登记待人处理，环境恢复后重新开始')
            return
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
            if self.external():
                raise ValueError('外部环境由使用者维护，看板不能为修复工作树另起环境；请用会话修复（sop-board fix prepare）')
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
            if self.external():
                # 外部环境不由看板启停：“重启环境”只重新探测，必要时执行自愈命令。
                self.check_external()
                return
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
            if (self.state.get('waiting_port') and self.approved() and not self.stopping
                    and time.monotonic() >= self.next_port_retry):
                try:
                    self.ensure_baseline()
                except PortBusy:
                    pass
        finally:
            self.operation.release()

    def public(self, local=False):
        state = dict(self.state)
        if self.process and self.process.poll() is not None:
            state['status'] = '已退出'
        record = self.latest_check()
        attempt = self.latest_check_attempt()
        approved = self.approved()
        checking = bool(self.check_thread and self.check_thread.is_alive())
        instructions = [] if approved else [
            '工作区 ' + self.workspace.name + ' 尚未搭建完成，或配置变化后尚未自检。在以数据目录为工作目录的代理会话中，按 sop-board guide 继续搭建或修正下列问题：',
            *self.workspace.setup_instructions(),
            '搭建后运行 sop-board check ' + self.workspace.name + ' --data ' + str(self.workspace.data) + '；通过后启用执行与修复。']
        if instructions and not local:
            instructions = ['尚未搭建，请开发者在本机处理']
        result = dict(status=state['status'], enabled=approved and not checking, checking=checking,
                      checked_at=record['finished_at'] if record else None, check_passed=approved,
                      setup_instructions=instructions,
                      ports=self.workspace.adapter['ports'] if self.workspace.configured else [])
        if self.workspace.configured and self.external():
            # 内网只显示探测项名称与是否通过，地址与说明只在本机显示。
            result['external'] = dict(probed_at=state.get('probed_at'), healed=state.get('healed', False),
                                      probes=[p if local else dict(name=p['name'], ok=p['ok']) for p in state.get('probes', [])])
        result['check_notice'] = ((attempt.get('message','自检未能在当前环境运行') if local else
            '自检未能在当前环境运行，请开发者在沙箱外处理；已完成的自检结果保留')
            if attempt and attempt.get('completed') is False else '')
        if local:
            lines = self.log.read_text(encoding='utf-8', errors='replace').splitlines()[-50:] if self.log and self.log.exists() else []
            result.update(state=state, latest_check=record, latest_check_attempt=attempt, log_lines=lines, problems=self.workspace.problems)
        return result

    def check(self):
        started = stamp()
        result = dict(started_at=started,config_digest=self.workspace.config_digest,ok=True,completed=True,steps=[],credentials=[])
        if not self.operation.acquire(blocking=False):
            return self.save_check(dict(result,ok=False,completed=False,reason='环境正在被执行或修复使用，不能开始自检'))
        try:
            if self.process:
                self._stop()
            self.workspace.reload()
            directory = self.workspace.data / 'checks'
            directory.mkdir(exist_ok=True)
            from .credentials import statuses
            result.update(config_digest=self.workspace.config_digest,credentials=statuses(self.workspace.credential_entries()))
            def step(name, action):
                if not result['ok']:
                    result['steps'].append(dict(name=name, status='未执行', message='前一步未通过' if result['completed'] else '前一步未能完成', log=None))
                    return
                try:
                    message, log = action()
                    result['steps'].append(dict(name=name, status='通过', message=message, log=log))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    result['ok'] = False
                    reason = unavailable_reason(error)
                    if reason:
                        result.update(completed=False,reason=reason)
                    result['steps'].append(dict(name=name, status='未能运行' if reason else '失败', message=str(error), log=getattr(error, 'log', str(self.log) if self.log else None)))
            def config():
                problems = [p for p in self.workspace.problems if p not in self.workspace.skill_problems]
                if problems:
                    raise ValueError('；'.join(p['field'] + '：' + p['problem'] for p in problems))
                for path in self.workspace.repos.values():
                    git(path, 'rev-parse', '--verify', 'HEAD')
                git_identities(self.workspace.repos)
                return '配置、仓库路径及 Git 提交身份合格', None
            def ports():
                self._acquire_ports()
                busy = port_owners(self.workspace.adapter['ports'])
                if busy:
                    raise ValueError('端口被占用：\n' + '\n'.join(str(p['port']) + '\n' + p['process'] for p in busy))
                return '声明端口均空闲', None
            def baseline():
                try:
                    self._start(self.workspace.repos)
                    self.check_addresses()
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
                    self.check_addresses(extra)
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
                        elif not established and not parent.is_symlink() and parent.is_dir() and not any(
                                p.is_relative_to(parent) or parent.is_relative_to(p) for p in registered):
                            # 建立失败时只删本次目录中的空目录；诊断文件、链接和未知现场原样保留。
                            for root, _, _ in os.walk(parent, topdown=False, followlinks=False):
                                path = Path(root)
                                if path.is_symlink():
                                    continue
                                try:
                                    path.rmdir()
                                except OSError as error:
                                    if error.errno not in (errno.ENOTEMPTY,errno.EEXIST,errno.ENOENT):
                                        raise
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        if failure:
                            raise WorktreeError(str(failure) + '；清理也失败：' + str(error), cleanup_log) from failure
                        raise
                if failure:
                    raise failure
                return '已建立并核对各仓工作树，环境就绪并清理；建立日志：' + str(build_log) + '；清理日志：' + str(cleanup_log), log
            def health():
                ok = self.check_external(alert=False)
                text = '；'.join(f"{p['name']} {'通过' if p['ok'] else '未通过'}（{p['message']}）" for p in self.state['probes'])
                if not ok:
                    raise ValueError(self.state['error'] + '：' + text)
                return ('外部环境由使用者维护，看板只探测：' + text + ('；首次探测未通过，已执行自愈命令' if self.state['healed'] else '')), None
            def skills():
                self.workspace.reload()
                if self.workspace.skill_problems:
                    raise ValueError('；'.join(p['field'] + '：' + p['problem'] for p in self.workspace.skill_problems))
                used = ['验证技能：' + str(self.workspace.skill)]
                for name in self.workspace.entries:
                    helper = self.workspace.driver_paths.get(name)
                    used.append(name + ('；辅助技能：' + helper if helper else '；辅助技能：无'))
                return '；'.join(used), None
            steps = ([('配置与仓库', config), ('外部环境探测', health), ('验证技能', skills)] if self.external() else
                     [('配置与仓库', config), ('端口', ports), ('基线环境', baseline), ('临时工作树环境', worktrees), ('验证技能', skills)])
            for name, action in steps:
                step(name, action)
            if self.workspace.reload() or result['config_digest'] != self.workspace.config_digest:
                result.update(ok=False,completed=True)
                result['steps'][0].update(status='失败', message='自检期间配置变化，请重新自检')
            return self.save_check(result)
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            reason = unavailable_reason(error)
            if not reason:
                raise
            return self.save_check(dict(result,ok=False,completed=False,reason=reason))
        finally:
            if not self.process:
                self._release_ports()
            self.operation.release()

    def start_check(self):
        with self.check_request_lock:
            if self.check_thread and self.check_thread.is_alive():
                raise ValueError('自检正在运行')
            if not self.operation.acquire(blocking=False):
                self.check()
                return
            self.operation.release()
            def run():
                try:
                    result = self.check()
                    if (result['ok'] or result.get('completed') is False and self.approved()) and not self.stopping:
                        with self.operation:
                            self.ensure_baseline()
                except PortBusy:
                    pass
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
