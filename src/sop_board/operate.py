"""经看板操作身份浏览器（已定决策 31 第二段）。

守护进程为每个身份持有一个 playwright-cli 驱动会话：执行会话只提交步骤，不自己接入、选标签页或保管连接。
一次调用可以带多步，按顺序执行，遇到失败即停；结束时返回当前页面结构（可按区域裁剪），每一步都记进运行目录。
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

from . import browsers, progress, redaction
from .errors import reject
from .store import now

# 会话与浏览器的生命周期由看板管理；清空存储会毁掉登录态。这些子命令不接受。
FORBIDDEN = frozenset((
    'open', 'attach', 'close', 'detach', 'delete-data', 'list', 'close-all', 'kill-all', 'install', 'install-browser',
    'state-load', 'cookie-clear', 'localstorage-clear', 'sessionstorage-clear', 'show'))
STEP_TIMEOUT = 30
MAX_STEP_TIMEOUT = 3600
MAX_STEPS = 50
INLINE_LIMIT = 20000
RECORD = 'steps.jsonl'
SECTION = re.compile(r'^### (.+)$', re.M)


def sections(text):
    """playwright-cli 的文字输出按 `### 标题` 分节。"""
    found, names = {}, SECTION.split(text)
    for name, body in zip(names[1::2], names[2::2]):
        found[name.strip()] = body.strip()
    return found


def fenced(body):
    """取出 ```yaml 代码块的内容；没有代码块时原样返回。"""
    match = re.search(r'```[a-z]*\n(.*?)\n?```', body, re.S)
    return match.group(1) if match else body


def page_of(parts):
    page = {}
    for line in parts.get('Page', '').splitlines():
        label, _, value = line.lstrip('- ').partition(': ')
        if label == 'Page URL':
            page['url'] = value
        elif label == 'Page Title':
            page['title'] = value
    return page


def validate(steps):
    if not isinstance(steps, list) or not steps:
        reject(422, 'invalid-arguments', 'steps', '至少给出一步')
    if len(steps) > MAX_STEPS:
        reject(422, 'invalid-arguments', 'steps', f'一次最多 {MAX_STEPS} 步')
    for number, step in enumerate(steps, 1):
        if not isinstance(step, list) or not step or any(not isinstance(part, str) for part in step):
            reject(422, 'invalid-arguments', 'steps', f'第 {number} 步须为非空的字符串数组（子命令与参数）')
        command = step[0]
        if command.startswith('-') or command in FORBIDDEN:
            reject(422, 'step-not-allowed', 'steps', f'第 {number} 步的子命令 {command} 不接受：会话、浏览器生命周期与清空存储由看板管理')


class Driver:
    def __init__(self, app):
        self.app = app
        self.guard = threading.Lock()
        self.locks = {}
        self.sessions = {}   # 身份 → (endpoint, 持有者)：变化时重新接入

    def lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def home(self, identity):
        directory = self.app.project.data / 'browsers' / identity / 'driver'
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def environment(self):
        base = self.app.project.data / 'browsers' / '.driver'
        env = browsers.runtime_env()
        env.update(PWTEST_DAEMON_SESSION_DIR=str(base / 'daemon'), PWTEST_SERVER_REGISTRY=str(base / 'registry'))
        return env

    def cli(self, identity, args, *, timeout, session=True):
        """运行一条驱动命令，返回 (是否成功, 各节)。超时或驱动自身出错都算这一步失败。"""
        node = shutil.which('node')
        script = browsers.runtime_dir() / 'node_modules' / '.bin' / 'playwright'
        if not node or not script.exists():
            reject(409, 'runtime-missing', 'runtime', '浏览器运行时未安装：请运行 sop-board browser install')
        command = [node, str(script), 'cli', *([f'-s={identity}'] if session else []), *args]
        try:
            done = subprocess.run(command, cwd=self.home(identity), env=self.environment(), capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, {'Error': f'这一步超过 {timeout} 秒没有结束，已中止'}
        text = done.stdout.strip() or done.stderr.strip()
        parts = sections(text)
        if not parts:
            parts = {'Result' if done.returncode == 0 else 'Error': text}
        return 'Error' not in parts and done.returncode == 0, parts

    def ensure(self, identity, endpoint, holder):
        """身份的驱动会话接在当前浏览器上；浏览器或持有者变了就重新接入。返回 (是否刚接入, 标签页列表)。"""
        if self.sessions.get(identity) == (endpoint, holder):
            return False, None
        self.cli(identity, ['detach'], timeout=STEP_TIMEOUT)
        ok, parts = self.cli(identity, ['attach', f'--cdp={endpoint}', f'--session={identity}'], timeout=STEP_TIMEOUT, session=False)
        if not ok:
            self.sessions.pop(identity, None)
            reject(409, 'browser-attach-failed', 'identity', f"接入身份 {identity} 的浏览器失败：{parts.get('Error', '')[:300]}")
        self.sessions[identity] = (endpoint, holder)
        _, tabs = self.cli(identity, ['tab-list'], timeout=STEP_TIMEOUT)
        return True, tabs.get('Result', '')

    def clean(self, identity):
        shutil.rmtree(self.home(identity) / '.playwright-cli', ignore_errors=True)

    def perform(self, context, work, identity, steps, *, region=None, depth=None, shot=False, snapshot=True,
                timeout=STEP_TIMEOUT):
        from . import runs
        validate(steps)
        run_id, directory = work['id'], Path(work['dir'])
        holder = f'run:{run_id}'
        with context.store.local() as db:
            row = db.execute('SELECT holder FROM browser_instances WHERE identity=?', (identity,)).fetchone()
        if row is None or row['holder'] != holder:
            reject(409, 'browser-not-held', 'identity', f'运行 #{run_id} 没有持有身份 {identity} 的浏览器')
        instance = self.app.browsers.instance(identity)
        if not instance.alive():
            reject(409, 'browser-not-running', 'identity', f'身份 {identity} 的浏览器没有在运行，按 brief 的浏览器问题处理')
        secrets = [os.environ[name] for name in redaction.names(self.app.project.entries) if os.environ.get(name)]
        hide = lambda text: redaction.redact(text.encode(), secrets).decode()
        with self.lock(identity):
            attached, tabs = self.ensure(identity, instance.endpoint(), holder)
            scenarios = [item['scenario'] for item in progress.in_progress(progress.read(work))]
            results, failed = [], None
            with self.lock(('record', run_id)):
                first = sum(1 for _ in (directory / RECORD).open(encoding='utf-8')) + 1 if (directory / RECORD).exists() else 1
            for offset, step in enumerate(steps):
                number, started = first + offset, now()
                ok, parts = self.cli(identity, step, timeout=timeout)
                if not ok and 'is not open' in parts.get('Error', ''):
                    # 浏览器重启过：重新接入后重做这一步。
                    self.sessions.pop(identity, None)
                    self.ensure(identity, instance.endpoint(), holder)
                    ok, parts = self.cli(identity, step, timeout=timeout)
                output = parts.get('Error') if not ok else (
                    fenced(parts['Snapshot']) if step[0] == 'snapshot' and 'Snapshot' in parts else parts.get('Result', ''))
                item = dict(n=number, at=started, command=step, ok=ok, duration_ms=progress.elapsed(started, now()), output=output or '')
                results.append(item)
                if not ok:
                    failed = number
                    break
            last = first + len(results) - 1
            final, page, files = None, {}, {}
            store = directory / 'steps'
            store.mkdir(exist_ok=True)
            if snapshot:
                args = ['snapshot', *([region] if region else []), *([f'--depth={depth}'] if depth else [])]
                ok, parts = self.cli(identity, args, timeout=timeout)
                page = page_of(parts)
                if ok and 'Snapshot' in parts:
                    text = fenced(parts['Snapshot'])
                    (store / f'{last}.yml').write_text(text + '\n', encoding='utf-8')
                    files['snapshot'] = f'steps/{last}.yml'
                    final = dict(text=text[:INLINE_LIMIT], file=files['snapshot'], truncated=len(text) > INLINE_LIMIT)
                else:
                    final = dict(text=None, file=None, truncated=False, error=parts.get('Error', '')[:500])
            if shot:
                target = store / f'{last}.png'
                ok, parts = self.cli(identity, ['screenshot', f'--filename={target}'], timeout=timeout)
                if ok and target.exists():
                    files['screenshot'] = f'steps/{last}.png'
            for item in results:
                if len(item['output']) > INLINE_LIMIT:
                    (store / f"{item['n']}.out.txt").write_text(item['output'], encoding='utf-8')
                    item.update(output=item['output'][:INLINE_LIMIT], output_file=f"steps/{item['n']}.out.txt", truncated=True)
            with self.lock(('record', run_id)), (directory / RECORD).open('a', encoding='utf-8') as stream:
                for item in results:
                    line = dict(n=item['n'], at=item.pop('at'), identity=identity, command=[hide(part) for part in item['command']],
                                ok=item['ok'], duration_ms=item['duration_ms'], url=page.get('url'), scenarios=scenarios)
                    if not item['ok']:
                        line['error'] = hide(item['output'])[:1000]
                    if item['n'] == last:
                        line.update(files)
                    stream.write(json.dumps(line, ensure_ascii=False) + '\n')
            self.clean(identity)
        return dict(ok=True, run=run_id, identity=identity, attached=attached, tabs=tabs, steps=results, failed=failed,
                    skipped=len(steps) - len(results), page=page, snapshot=final, screenshot=files.get('screenshot'),
                    in_progress=scenarios, warnings=runs.touch(context, run_id))


def attach(app):
    app.driver = Driver(app)


def _register():
    from .server import json_response, route

    @route('POST', '/local/browser/do', 'token')
    def do(app, request):
        from . import runs
        from .cli import Context
        run, identity = request.json_field('run'), request.json_field('identity') or 'default'
        if type(run) is not int:
            reject(422, 'run-required', 'run', '请指定 --run 或载入运行目录的 run.env')
        timeout = request.json_field('timeout')
        if timeout is None:
            timeout = STEP_TIMEOUT
        depth = request.json_field('depth')
        if type(timeout) is not int or not 1 <= timeout <= MAX_STEP_TIMEOUT:
            reject(422, 'invalid-arguments', 'timeout', f'每步超时须为 1～{MAX_STEP_TIMEOUT} 的整数秒')
        if depth is not None and (type(depth) is not int or depth < 1):
            reject(422, 'invalid-arguments', 'depth', '页面结构的层数须为正整数')
        region = request.json_field('region')
        if region is not None and (not isinstance(region, str) or not region or region.startswith('-')):
            reject(422, 'invalid-arguments', 'region', '区域须为选择器或页面结构里的引用')
        context = Context(app.project, app.store, app.checkout())
        work = runs.load_work(context, run)
        if work['status'] != 'prepared' or not work['dir']:
            reject(409, 'run-not-prepared', 'run', f'运行 #{run} 尚未准备或已经结束')
        return json_response(app.driver.perform(
            context, work, identity, request.json_field('steps'), region=region, depth=depth,
            shot=bool(request.json_field('shot')), snapshot=request.json_field('snapshot') is not False, timeout=timeout))


_register()
