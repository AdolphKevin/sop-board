"""看板守护进程的状态文件与本机客户端；服务端在 server.py。

浏览器与环境进程都由守护进程启动；它可以在终端或 agent 会话中运行。
CLI 需要环境或浏览器时，通过回环 HTTP 调用它，以数据目录中的本机令牌鉴权。
"""
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

def state_path(project):
    return project.data / 'daemon.json'


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
    return dict(running=True, pid=state['pid'], url=state.get('url'), lan=state.get('lan', []),
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
    """以本机令牌调用守护进程的 /local/ 接口；返回 JSON。"""
    state = describe(project)
    if not state['running']:
        raise DaemonUnavailable(f'看板守护进程没有运行：请运行 sop-board serve --project {project.name}')
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
        raise DaemonUnavailable('无法连接看板守护进程：' + str(error)) from error
