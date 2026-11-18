"""在当前检出的就绪环境中运行入口声明的只读探测。"""
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .entries import PROBE_TIMEOUT
from .errors import APIError, reject
from .redaction import StreamRedactor, names, redact
from .store import dumps, now


def timeout(entries):
    return sum(entry.get('probe_timeout', PROBE_TIMEOUT) for entry in entries if 'probe' in entry)


def execute(argv, *, cwd, env, seconds, secrets):
    started = time.monotonic()
    code, reason = None, None
    tail = b''
    redactor = StreamRedactor(secrets)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        reason = redact(f'无法启动探测：{error}'.encode(), secrets).decode()
    else:
        deadline = started + seconds
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while selector.select(max(0, deadline - time.monotonic())):
                    chunk = os.read(process.stdout.fileno(), 16384)
                    if not chunk:
                        code = process.wait(timeout=max(0, deadline - time.monotonic()))
                        break
                    tail = (tail + redactor.feed(chunk))[-16384:]
                    if time.monotonic() >= deadline:
                        break
                if code is None:
                    raise subprocess.TimeoutExpired(argv, seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                reason = f'探测超过 {seconds} 秒'
        tail += redactor.feed(b'', final=True)
        if code is not None and code < 0:
            reason = f'探测被信号 {-code} 结束'
    output = '\n'.join(tail.decode('utf-8', 'replace').strip().splitlines()[-10:])
    if output:
        prefix = f'{reason}：' if reason else ''
        reason = prefix + output[-(2000 - len(prefix)):]
    elif reason is None and code != 0:
        reason = f'探测退出码 {code}'
    return dict(status='healthy' if code == 0 else 'unhealthy', reason=reason[-2000:] if reason else None,
        duration_ms=int((time.monotonic() - started) * 1000), exit_code=code if code is not None and code >= 0 else None)


def batch(manager, checkout, requested, *, ready_only=False):
    """持有检出环境锁，按同一份就绪变量运行一批。启动预检可略过未就绪的环境。"""
    project = manager.project
    entries = project.entries
    keys = sorted(set(requested) if requested else entries)
    unknown = [name for name in keys if name not in entries]
    if unknown:
        raise APIError(422, 'entry-unknown', [dict(field='entry', problem=f'没有声明入口 {name}') for name in unknown])
    declared = any('probe' in entries[name] for name in keys)
    with manager.lock(checkout):
        if ready_only or declared:
            state = manager.status(checkout)
            if state['state'] != 'ready':
                if ready_only:
                    return []
                reject(409, 'env-not-ready', 'environment', f'检出 {checkout.name} 的环境尚未就绪')
        if declared:
            environment = manager.environment(checkout)
            env = environment.inputs(project.data / 'env' / checkout.name)
            env.update(state['variables'])
            env.update(state['address_env'])
            credential_names = set(names(entries))
            if project.fixtures:
                credential_names.update(project.fixtures.get('credential_env', []))
            secrets = {values[name] for name in credential_names for values in (os.environ, env) if values.get(name)}
        reports = []
        for name in keys:
            entry = entries[name]
            at = now()
            if 'probe' in entry:
                report = execute(environment.command(entry['probe']), cwd=project.data, env=env,
                                 seconds=entry.get('probe_timeout', PROBE_TIMEOUT), secrets=secrets)
            else:
                report = dict(status='undeclared', duration_ms=None, exit_code=None, reason=None)
            reports.append(dict(entry=name, at=at, **report))
    return reports


def unhealthy(reports, **details):
    problems = [dict(field=f"entries.{p['entry']}.probe", problem=f"{p['entry']}：{p['reason']}")
                for p in reports if p['status'] == 'unhealthy']
    return APIError(409, 'entry-unhealthy', problems, probes=reports, **details) if problems else None


def excluded(candidates, reports, *, fixing=False):
    failures = {p['entry']: p for p in reports if p['status'] == 'unhealthy'}
    skipped = [dict(scenario=item['scenario'], fingerprint=item['fingerprint'], cause='environment',
                    finding=f"入口 {item['entry']} 不可用：{failures[item['entry']]['reason']}")
               for item in candidates if item['entry'] in failures]
    if skipped and (fixing or len(skipped) == len(candidates)):
        raise unhealthy(reports, probe_skipped=skipped)
    return skipped


def append(directory, source, reports):
    with (Path(directory) / 'probes.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(dumps(dict(source=source, at=now(), probes=reports)) + '\n')
