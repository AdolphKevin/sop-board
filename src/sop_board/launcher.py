"""无头会话启动器（守护进程侧）：用同一份 sop-board 技能在检出目录启动 claude -p 或 codex exec。

只记录进程号、日志、会话编号与退出码；会话自己经 CLI 准备与提交。同一检出同时只运行一个启动。
启动器取得的会话编号就是独立执行的证据：Claude 由 --session-id 指定，Codex 取 thread.started。
会话输出经随会话存活的转写进程（relay.py）写进日志，不经守护进程；sop-board restart 时会话照常运行，
新守护进程按进程号、启动时间与命令行认回（已定决策 28）。
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

from .errors import APIError
from .store import dumps, now

KINDS = ('run', 'distill')


def agent_for(project, kind):
    return project.agents.get(kind) or project.agents.get('default') or dict(runner='codex')


def work_directory(project, checkout):
    return checkout.paths[project.scenario_repo].resolve()


def prompt_for(project, checkout, kind, scope, implementer, fix=None, implemented_by=None):
    if fix is not None:
        command = f'sop-board run prepare --wait 540 --fix {fix}'
    elif kind == 'run':
        command = 'sop-board run prepare --wait 540 --scope ' + ' '.join(scope)
    else:
        command = 'sop-board distill prepare ' + ' '.join(scope)
    text = Template(files('sop_board').joinpath('prompts', 'launch.md').read_text(encoding='utf-8')).substitute(
        project=project.name, checkout=checkout.name, directory=work_directory(project, checkout),
        kind='场景执行' if kind == 'run' else '回归沉淀', scope='、'.join(scope), command=command,
        implementer=f'提交时加 --implementer {implementer}。' if implementer
        else f'提交时加 --implemented-by {implemented_by}（实现者是别的操作者，跨机器验收）。' if implemented_by else '')
    return text


def running_launch(db, checkout):
    """同一检出同时只运行一个启动。"""
    running = db.execute("SELECT id FROM launches WHERE checkout=? AND status='running'", (checkout.id,)).fetchone()
    if running:
        raise APIError(409, 'launch-running', [dict(field='launch', problem=
            f"检出 {checkout.name} 已有运行中的启动 #{running['id']}；结束后再启动，或运行 sop-board launch stop --launch {running['id']}")])


class Launcher:
    def __init__(self, project, store):
        self.project, self.store = project, store
        self.processes = {}
        self.adopted = []   # 本进程启动时认回的启动（restart 交接或看板被强杀后仍在运行的会话）
        self.guard = threading.Lock()

    def start(self, checkout, kind, scope, *, requested_by, implementer=None, fix=None, implemented_by=None):
        if kind not in KINDS:
            raise ValueError('启动类型应为 run 或 distill')
        if not scope:
            raise ValueError('须给出场景')
        self.project.require()
        # 已有运行中的启动时先报 launch-running：它可能已准备了运行，范围检查会把那些场景当成别的运行占着的。
        with self.store.local() as db:
            running_launch(db, checkout)
        from .cli import Context
        context = Context(self.project, self.store, checkout)
        excluded = []
        if kind == 'run':
            from .runs import validate_scope, invalid_files
            found, _, _, selected = validate_scope(context, scope)
            excluded = invalid_files(found)
            scope = [item['scenario'] for item in selected]
        else:
            from .guards import validate_distill_scope
            validate_distill_scope(context, scope)
        agent = agent_for(self.project, kind)
        directory = self.project.data / 'launches'
        directory.mkdir(exist_ok=True)
        with self.guard:
            with self.store.local_tx() as db:
                running_launch(db, checkout)
                cursor = db.execute(
                    "INSERT INTO launches(checkout,kind,agent,scope,log,status,requested_by,started_at) VALUES (?,?,?,?,?,'running',?,?)",
                    (checkout.id, kind, agent['runner'], dumps(scope), '', requested_by, now()))
                launch_id = cursor.lastrowid
            from . import workdirs
            directory = workdirs.create(directory, launch_id)
            log = directory / 'output.jsonl'
            prompt = prompt_for(self.project, checkout, kind, scope, implementer, fix, implemented_by)
            log.with_suffix('.prompt.md').write_text(prompt, encoding='utf-8')
            session = str(uuid.uuid4()) if agent['runner'] == 'claude' else None
            env = dict(os.environ, SOP_BOARD_LAUNCH=str(launch_id), SOP_BOARD_CHECKOUT=checkout.name)
            env.pop('SOP_BOARD_SESSION', None)
            if session:
                env['SOP_BOARD_SESSION'] = session
            env.pop('SOP_BOARD_IMPLEMENTER', None)
            env.pop('SOP_BOARD_IMPLEMENTED_BY', None)
            if implementer:
                env['SOP_BOARD_IMPLEMENTER'] = implementer
            elif implemented_by:
                env['SOP_BOARD_IMPLEMENTED_BY'] = implemented_by
            work = work_directory(self.project, checkout)
            # 会话写项目工作目录与证据；浏览器和 Node 依赖由守护进程准备，只读使用。
            writable = [str(path.resolve()) for path in (self.project.data, self.project.data.parent / 'evidence')]
            argv = self.argv(agent, prompt, work, writable, session)
            try:
                process = self.spawn(argv, work, env, log)
            except OSError as error:
                log.write_text(f"无法启动 {agent['runner']}：{error}\n", encoding='utf-8')
                with self.store.local_tx() as db:
                    db.execute("UPDATE launches SET status='failed', log=?, ended_at=? WHERE id=?", (str(log), now(), launch_id))
                raise ValueError(f"无法启动 {agent['runner']}：{error}") from error
            with self.store.local_tx() as db:
                db.execute('UPDATE launches SET pid=?, log=?, session=? WHERE id=?', (process.pid, str(log), session, launch_id))
            self.processes[launch_id] = process
            threading.Thread(target=self.watch, args=(launch_id, process, log), daemon=True, name=f'launch-{launch_id}').start()
        result = self.read(launch_id)
        result['invalid_files'] = excluded
        return result

    def spawn(self, argv, work, env, log):
        """先起转写进程读管道，再起会话写管道；守护进程不持有管道两端，自己退出也不影响会话写日志。"""
        read_end, write_end = os.pipe()
        relay = None
        try:
            relay = subprocess.Popen([sys.executable, '-m', 'sop_board.relay', '--data', str(self.project.data), '--log', str(log)],
                                     stdin=read_end, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                process = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=write_end,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                os.close(write_end)
                write_end = None
                # 等转写进程读到文件尾退出，再由调用方写入失败原因，免得被它清空。
                try:
                    relay.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    relay.kill()
                raise
        finally:
            os.close(read_end)
            if write_end is not None:
                os.close(write_end)
        threading.Thread(target=relay.wait, daemon=True, name='launch-relay').start()
        return process

    def argv(self, agent, prompt, work, writable, session):
        if agent['runner'] == 'claude':
            argv = [sys.executable, '-m', 'sop_board.claude_runner', '--directory', str((self.project.data / 'launches').resolve()),
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
        adopted = not isinstance(process, subprocess.Popen)
        while process.poll() is None:
            if session is None:
                session = self.session_from_log(log)
                if session:
                    with self.store.local_tx() as db:
                        db.execute('UPDATE launches SET session=COALESCE(session, ?) WHERE id=?', (session, launch_id))
            time.sleep(1)
        # 认回的会话不是本进程的子进程，取不到退出码：记 exited，有没有产出运行由 public() 判断（没有即 no-work）。
        code = None if adopted else process.returncode
        session = session or self.session_from_log(log)
        with self.store.local_tx() as db:
            row = db.execute('SELECT status FROM launches WHERE id=?', (launch_id,)).fetchone()
            if row and row['status'] == 'stopped':
                status = 'stopped'
            else:
                status = 'exited' if adopted or code == 0 else 'failed'
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
        with self.store.local_tx() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            if row is None:
                raise APIError(404, 'launch-not-found', [dict(field='launch', problem=f'启动不存在：#{launch_id}')])
            if row['status'] != 'running':
                return self.read(launch_id)
            db.execute("UPDATE launches SET status='stopped' WHERE id=?", (launch_id,))
        process = self.processes.get(launch_id)
        if process and process.poll() is None:
            try:
                if isinstance(process, subprocess.Popen):
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    # 认回的会话：发信号前重新核对启动时间与命令行，防止进程号已被别的进程复用。
                    from .browsers import process_snapshot
                    if process_snapshot(process.pid) == process.snapshot:
                        os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return self.read(launch_id)

    def read(self, launch_id, *, local=True):
        with self.store.local() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            runs = [dict(r) for r in db.execute('SELECT id, kind, status, session FROM local_work WHERE launch=?', (launch_id,))]
        return public(row, runs, local=local) if row else None

    def recover(self):
        """守护进程启动时：上次运行中的启动若进程还在、核对是同一个会话，就认回并继续监视；否则记为 failed。"""
        with self.store.local() as db:
            rows = [dict(r) for r in db.execute("SELECT id, pid, agent, log, started_at FROM launches WHERE status='running'")]
        adopted = []
        for row in rows:
            process = adoptable(row)
            if process is None:
                with self.store.local_tx() as db:
                    db.execute("UPDATE launches SET status='failed', ended_at=? WHERE id=?", (now(), row['id']))
                continue
            self.processes[row['id']] = process
            threading.Thread(target=self.watch, args=(row['id'], process, Path(row['log'])), daemon=True,
                             name=f"launch-{row['id']}").start()
            adopted.append(dict(launch=row['id'], pid=row['pid']))
            print(f"已认回启动 #{row['id']}：会话进程 {row['pid']}", flush=True)
        self.adopted = adopted
        return adopted

    def running(self):
        """正在运行的启动：编号、检出与会话进程号。"""
        with self.store.local() as db:
            return [dict(launch=r['id'], checkout=r['name'], pid=r['pid'])
                    for r in db.execute("SELECT l.id, l.pid, c.name FROM launches l JOIN checkouts c ON c.id=l.checkout "
                                        "WHERE l.status='running' ORDER BY l.id")
                    if r['id'] in self.processes]

    def shutdown(self, *, handover=False):
        """正常停止时结束全部会话；restart 交接时会话照常运行，由新守护进程认回。"""
        if handover:
            kept = self.running()
            for item in kept:
                print(f"已保留启动 #{item['launch']}：会话进程 {item['pid']}", flush=True)
            return kept
        for launch_id in list(self.processes):
            self.stop(launch_id)
        return []


def adoptable(row):
    """进程还在，命令行是这种会话，启动时间与启动记录吻合，才认回；否则返回 None。"""
    from .browsers import RecoveredProcess, process_snapshot
    from .status import instant
    if not row['pid'] or not row['log']:
        return None
    snapshot = process_snapshot(row['pid'])
    if not snapshot:
        return None
    marker = 'sop_board.claude_runner' if row['agent'] == 'claude' else 'codex'
    if marker not in snapshot[1]:
        return None
    try:
        process = RecoveredProcess(row['pid'], snapshot)
        launched = instant(row['started_at']).timestamp()
    except (ValueError, TypeError):
        return None
    # ps 的启动时间精确到秒；会话在登记启动记录之后几秒内启动。
    return process if launched - 5 <= process.started <= launched + 120 else None


def public(row, runs, *, local):
    item = dict(id=row['id'], kind=row['kind'], agent=row['agent'], scope=json.loads(row['scope']), status=row['status'],
                exit_code=row['exit_code'], requested_by=row['requested_by'], started_at=row['started_at'],
                ended_at=row['ended_at'], checkout=row['checkout'], runs=[dict(id=r['id'], kind=r['kind'], status=r['status']) for r in runs])
    if row['status'] == 'exited' and not runs:
        item.update(status='no-work', problem=f"启动 #{row['id']} 未产生运行（或沉淀）")
    elif row['status'] == 'failed':
        item['problem'] = f"启动 #{row['id']} 失败" + ('' if runs else '，未产生运行（或沉淀）')
    if row['status'] in ('exited', 'failed'):
        unsubmitted = [dict(id=r['id'], kind=r['kind'], status=r['status'])
                       for r in runs if r['status'] in ('prepared', 'abandoned')]
        if unsubmitted:
            item['unsubmitted'] = unsubmitted
            details = '、'.join(
                f"{'运行' if r['kind'] == 'run' else '沉淀'} #{r['id']} "
                + ('未提交（prepared）' if r['status'] == 'prepared' else '已放弃（abandoned，未登记结果）')
                for r in unsubmitted)
            item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + '；' + details
    if item['status'] in ('no-work', 'failed') or item.get('unsubmitted'):
        if local:
            try:
                with Path(row['log']).open('rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 8192))
                    item['log_tail'] = stream.read().decode('utf-8', errors='replace')[-4000:]
            except OSError:
                item['log_tail'] = '启动日志不可读'
    if local:
        item.update(pid=row['pid'], log=row['log'], session=row['session'],
                    prompt=str(Path(row['log']).with_suffix('.prompt.md')) if row['log'] else None)
    return item


# ---- 守护进程挂载与接口 ----

def attach(app):
    from . import extensions
    app.launcher = Launcher(app.project, app.store)
    extensions.RECOVER.append(lambda app: app.launcher.recover())
    extensions.CLOSE.append(lambda app: app.launcher.shutdown(handover=app.handover))


def _register():
    from .server import json_response, route

    @route('POST', '/local/launch', 'token')
    def launch(app, request):
        checkout = app.checkout(request.json_field('checkout'))
        return json_response(dict(ok=True, launch=app.launcher.start(
            checkout, request.json_field('kind') or 'run', request.json_field('scope') or [],
            requested_by=request.json_field('requested_by') or '本机', implementer=request.json_field('implementer'),
            implemented_by=request.json_field('implemented_by'), fix=request.json_field('fix'))))

    @route('POST', '/local/launch/stop', 'token')
    def stop(app, request):
        return json_response(dict(ok=True, launch=app.launcher.stop(int(request.json_field('launch')))))


_register()
