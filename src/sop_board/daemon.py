"""看板守护进程的状态文件与本机客户端；服务端在 server.py。

浏览器与环境进程都由守护进程启动；它可以在终端或 agent 会话中运行。
CLI 需要环境或浏览器时，通过回环 HTTP 调用它，以数据目录中的本机令牌鉴权。
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

HANDOVER_SECONDS = 120  # restart 期间 CLI 最多等新守护进程这么久（已定决策 28）


def state_path(project):
    return project.data / 'daemon.json'


def handover_path(project):
    """sop-board restart 的交接记录：旧守护进程写下保留的浏览器、会话、环境与运行，新守护进程读取后删除。"""
    return project.data / 'restart.json'


def write_handover(project, value):
    path = handover_path(project)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_handover(project, *, from_pid=None):
    """交接记录：由 from_pid 交接给本进程的照旧有效；其余的写下不到 HANDOVER_SECONDS 秒才算正在重启。"""
    try:
        value = json.loads(handover_path(project).read_text(encoding='utf-8'))
        at = datetime.fromisoformat(value['at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if from_pid is not None and value.get('from_pid') == from_pid:
        return value
    if (datetime.now(timezone.utc) - at).total_seconds() > HANDOVER_SECONDS:
        return None
    return value


def clear_handover(project):
    handover_path(project).unlink(missing_ok=True)


def wait_exit(pid, timeout):
    deadline = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(.2)
    return True


def wait_restart(project):
    """正在 restart 时等新守护进程写好状态文件（最多 HANDOVER_SECONDS 秒）；没在重启时立即返回 describe() 的结果。"""
    while True:
        state = describe(project)
        handover = read_handover(project)
        if handover is None or (state['running'] and state['pid'] != handover.get('from_pid')):
            return state
        time.sleep(.5)


def token_path(project):
    return project.data / 'daemon.token'


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe(project):
    try:
        state = json.loads(state_path(project).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(running=False)
    if not isinstance(state, dict) or type(state.get('pid')) is not int or not alive(state['pid']):
        return dict(running=False)
    return dict(running=True, pid=state['pid'], port=state.get('port'), url=state.get('url'), lan=state.get('lan', []),
                started_at=state.get('started_at'))


def write_state(project, pid, port, lan, started_at):
    token = secrets.token_urlsafe(32)
    path = token_path(project)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
        stream.write(token)
    state = dict(pid=pid, port=port, url=f'http://127.0.0.1:{port}', lan=lan, started_at=started_at)
    state_path(project).write_text(json.dumps(state, ensure_ascii=False) + '\n', encoding='utf-8')
    return token


def clear_state(project, pid):
    try:
        state = json.loads(state_path(project).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if state.get('pid') == pid:
        state_path(project).unlink(missing_ok=True)
        token_path(project).unlink(missing_ok=True)


class DaemonUnavailable(ValueError):
    pass


def call(project, method, path, body=None, *, timeout=60):
    """以本机令牌调用守护进程的 /local/ 接口；返回 JSON。

    正在 sop-board restart 时先等新守护进程就绪；请求被拒绝连接（守护进程没收到）且正在重启时，等它就绪后重发一次。"""
    for attempt in (1, 2):
        state = wait_restart(project)
        if not state['running']:
            raise DaemonUnavailable(f'看板守护进程没有运行：请运行 {project.serve_command()}')
        try:
            token = token_path(project).read_text(encoding='ascii').strip()
        except OSError as error:
            raise DaemonUnavailable('无法读取守护进程令牌：' + str(error)) from error
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = Request(state['url'] + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b'{}')
        except HTTPError as error:
            try:
                payload = json.loads(error.read() or b'{}')
            except ValueError:
                payload = {}
            from .errors import APIError
            raise APIError(error.code, payload.get('error_code', 'daemon-error'),
                           payload.get('problems') or [dict(field='daemon', problem=f'HTTP {error.code}')]) from error
        except (URLError, OSError) as error:
            reason = getattr(error, 'reason', error)
            if read_handover(project) is not None:
                if attempt == 1 and isinstance(reason, ConnectionRefusedError):
                    continue
                raise DaemonUnavailable('看板在处理这条请求时重启了：请等它就绪后重新运行这条命令') from error
            raise DaemonUnavailable('无法连接看板守护进程：' + str(error)) from error
