"""无头会话启动器（守护进程侧）：用同一份 sop-board 技能在检出目录启动 claude -p 或 codex exec。

只记录进程号、日志、会话编号与退出码；会话自己经 CLI 准备与提交。同一检出同时只运行一个启动。
启动器取得的会话编号就是独立执行的证据：Claude 由 --session-id 指定，Codex 取 thread.started。
"""
from importlib.resources import files
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from string import Template

from . import browsers
from .errors import APIError
from .store import dumps, now

KINDS = ('run', 'distill')


def agent_for(project, kind):
    return project.agents.get(kind) or project.agents.get('default') or dict(runner='codex')


def work_directory(project, checkout):
    return checkout.paths[project.scenario_repo]


def prompt_for(project, checkout, kind, scope, implementer):
    command = ('sop-board run prepare --scope ' + ' '.join(scope)) if kind == 'run' else \
        ('sop-board distill prepare ' + ' '.join(scope))
    text = Template(files('sop_board').joinpath('prompts', 'launch.md').read_text(encoding='utf-8')).substitute(
        project=project.name, checkout=checkout.name, directory=work_directory(project, checkout),
        kind='场景执行' if kind == 'run' else '回归沉淀', scope='、'.join(scope), command=command,
        implementer=f'提交时加 --implementer {implementer}。' if implementer else '')
    return text


class Launcher:
    def __init__(self, project, store):
        self.project, self.store = project, store
        self.processes = {}
        self.guard = threading.Lock()

    def start(self, checkout, kind, scope, *, requested_by, implementer=None):
        if kind not in KINDS:
            raise ValueError('启动类型应为 run 或 distill')
        if not scope:
            raise ValueError('须给出场景')
        self.project.require()
        agent = agent_for(self.project, kind)
        directory = self.project.data / 'launches'
        directory.mkdir(exist_ok=True)
        with self.guard:
            with self.store.transaction() as db:
                running = db.execute("SELECT id FROM launches WHERE checkout=? AND status='running'", (checkout.id,)).fetchone()
                if running:
                    raise APIError(409, 'launch-running', [dict(field='launch', problem=
                        f"检出 {checkout.name} 已有运行中的启动 #{running['id']}；结束后再启动，或运行 sop-board launch stop --launch {running['id']}")])
                cursor = db.execute(
                    "INSERT INTO launches(checkout,kind,agent,scope,log,status,requested_by,started_at) VALUES (?,?,?,?,?,'running',?,?)",
                    (checkout.id, kind, agent['runner'], dumps(scope), '', requested_by, now()))
                launch_id = cursor.lastrowid
            log = directory / f'{launch_id}.jsonl'
            prompt = prompt_for(self.project, checkout, kind, scope, implementer)
            (directory / f'{launch_id}.prompt.md').write_text(prompt, encoding='utf-8')
            session = str(uuid.uuid4()) if agent['runner'] == 'claude' else None
            env = dict(os.environ, SOP_BOARD_LAUNCH=str(launch_id), SOP_BOARD_CHECKOUT=checkout.name)
            env.pop('SOP_BOARD_SESSION', None)
            if session:
                env['SOP_BOARD_SESSION'] = session
            if implementer:
                env['SOP_BOARD_IMPLEMENTER'] = implementer
            work = work_directory(self.project, checkout)
            writable = [str(self.project.data), str(browsers.runtime_dir())]
            argv = self.argv(agent, prompt, work, writable, session)
            try:
                with log.open('wb') as output:
                    process = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                               stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                with self.store.transaction() as db:
                    db.execute("UPDATE launches SET status='failed', log=?, ended_at=? WHERE id=?", (str(log), now(), launch_id))
                raise ValueError(f"无法启动 {agent['runner']}：{error}") from error
            with self.store.transaction() as db:
                db.execute('UPDATE launches SET pid=?, log=?, session=? WHERE id=?', (process.pid, str(log), session, launch_id))
            self.processes[launch_id] = process
            threading.Thread(target=self.watch, args=(launch_id, process, log), daemon=True, name=f'launch-{launch_id}').start()
        return self.read(launch_id)

    def argv(self, agent, prompt, work, writable, session):
        if agent['runner'] == 'claude':
            argv = [sys.executable, '-m', 'sop_board.claude_runner', '--directory', str(self.project.data / 'launches'),
                    '--session', session, '--work-directory', str(work)]
            if agent.get('model'):
                argv += ['--model', agent['model']]
            if agent.get('effort'):
                argv += ['--effort', agent['effort']]
            for path in writable:
                argv += ['--add-dir', path]
            argv += [prompt, '--'] + (agent.get('args') or ['--permission-mode', 'acceptEdits', '--allowedTools',
                                                             'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep'])
            return argv
        argv = ['codex', 'exec', '--json', '-s', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true',
                '--skip-git-repo-check', '-C', str(work)]
        for path in writable:
            argv += ['--add-dir', path]
        return argv + [prompt]

    def watch(self, launch_id, process, log):
        session = None
        while process.poll() is None:
            if session is None:
                session = self.session_from_log(log)
                if session:
                    with self.store.transaction() as db:
                        db.execute('UPDATE launches SET session=COALESCE(session, ?) WHERE id=?', (session, launch_id))
            time.sleep(1)
        code = process.returncode
        session = session or self.session_from_log(log)
        with self.store.transaction() as db:
            row = db.execute('SELECT status FROM launches WHERE id=?', (launch_id,)).fetchone()
            status = 'stopped' if row and row['status'] == 'stopped' else ('exited' if code == 0 else 'failed')
            db.execute('UPDATE launches SET status=?, exit_code=?, ended_at=?, session=COALESCE(session, ?) WHERE id=?',
                       (status, code, now(), session, launch_id))
        self.processes.pop(launch_id, None)

    @staticmethod
    def session_from_log(log):
        try:
            with log.open('rb') as stream:
                for _ in range(200):
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get('type') == 'thread.started' and event.get('thread_id'):
                        return str(event['thread_id'])
        except OSError:
            return None
        return None

    def stop(self, launch_id):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            if row is None:
                raise APIError(404, 'launch-not-found', [dict(field='launch', problem=f'启动不存在：#{launch_id}')])
            if row['status'] != 'running':
                return self.read(launch_id)
            db.execute("UPDATE launches SET status='stopped' WHERE id=?", (launch_id,))
        process = self.processes.get(launch_id)
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return self.read(launch_id)

    def read(self, launch_id, *, local=True):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            runs = [dict(r) for r in db.execute('SELECT id, kind, status, session FROM work WHERE launch=?', (launch_id,))]
        return public(row, runs, local=local) if row else None

    def recover(self):
        """守护进程启动时：上次运行中的启动若进程已不在，记为 failed。"""
        from .daemon import alive
        with self.store.transaction() as db:
            for row in db.execute("SELECT id, pid FROM launches WHERE status='running'").fetchall():
                if not row['pid'] or not alive(row['pid']):
                    db.execute("UPDATE launches SET status='failed', ended_at=? WHERE id=?", (now(), row['id']))

    def shutdown(self):
        for launch_id in list(self.processes):
            self.stop(launch_id)


def public(row, runs, *, local):
    item = dict(id=row['id'], kind=row['kind'], agent=row['agent'], scope=json.loads(row['scope']), status=row['status'],
                exit_code=row['exit_code'], requested_by=row['requested_by'], started_at=row['started_at'],
                ended_at=row['ended_at'], checkout=row['checkout'], runs=[dict(id=r['id'], kind=r['kind'], status=r['status']) for r in runs])
    if local:
        item.update(pid=row['pid'], log=row['log'], session=row['session'],
                    prompt=str(Path(row['log']).with_suffix('.prompt.md')) if row['log'] else None)
    return item


# ---- 守护进程挂载与接口 ----

def attach(app):
    from . import extensions
    app.launcher = Launcher(app.project, app.store)
    extensions.RECOVER.append(lambda app: app.launcher.recover())
    extensions.CLOSE.append(lambda app: app.launcher.shutdown())


def _register():
    from .server import json_response, route

    @route('POST', '/local/launch', 'token')
    def launch(app, request):
        checkout = app.checkout(request.json_field('checkout'))
        return json_response(dict(ok=True, launch=app.launcher.start(
            checkout, request.json_field('kind') or 'run', request.json_field('scope') or [],
            requested_by=request.json_field('requested_by') or '本机', implementer=request.json_field('implementer'))))

    @route('POST', '/local/launch/stop', 'token')
    def stop(app, request):
        return json_response(dict(ok=True, launch=app.launcher.stop(int(request.json_field('launch')))))


_register()
