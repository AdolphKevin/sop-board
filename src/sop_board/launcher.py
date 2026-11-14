"""无头会话启动器（守护进程侧）：用同一份 sop-board 技能在检出目录启动 claude -p 或 codex exec。

会话自己经 CLI 准备与提交，同一检出同时只运行一个启动。run 启动异常退出后接续一次，
最终结束时放弃尚未提交的运行并登记环境问题；尝试与接续时间保存在启动目录。
启动器取得的会话编号就是独立执行的证据：Claude 由 --session-id 指定，Codex 取 thread.started。
会话输出经随会话存活的转写进程（relay.py）写进日志，不经守护进程；sop-board restart 时会话照常运行，
新守护进程按进程号、启动时间与命令行认回（已定决策 28）。
"""
from datetime import datetime, timedelta
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
from .redaction import hide_local_paths, redact
from .store import dumps, now

KINDS = ('run', 'distill')
LAST_LINE_LIMIT = 300   # 写进待人处理的日志末尾一行的最大字数


def read_attempts(row):
    if row['kind'] != 'run' or not row['log']:
        return []
    try:
        return json.loads((Path(row['log']).parent / 'attempts.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []


def write_attempts(log, attempts):
    path = Path(log).parent / 'attempts.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(dumps(attempts) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def log_tail(log):
    try:
        with Path(log).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 8192))
            return redact(stream.read(), []).decode('utf-8', errors='replace')[-4000:]
    except OSError:
        return '启动日志不可读'


def finishing(attempts):
    return bool(attempts and attempts[-1].get('cleanup') and not attempts[-1]['cleanup'].get('complete'))


def error_text(error):
    message = '；'.join(p['problem'] for p in error.problems) if isinstance(error, APIError) else str(error)
    return redact(message.encode(), []).decode()


def state(db, row, *, local):
    # 先读完成标记，再读运行：完成标记出现时，运行的放弃与登记编号已经写好。
    attempts = read_attempts(row)
    runs = [dict(r) for r in db.execute('SELECT id, kind, status, session, result FROM local_work WHERE launch=?', (row['id'],))]
    return public(row, runs, local=local, attempts=attempts), runs


def agent_for(project, kind):
    return project.agents.get(kind) or project.agents.get('default') or dict(runner='codex')


def work_directory(project, checkout):
    return checkout.paths[project.scenario_repo].resolve()


def prompt_for(project, checkout, kind, scope, implementer, fix=None, implemented_by=None, *, allow_open_fix=False):
    if fix is not None:
        command = f'sop-board run prepare --wait 540 --fix {fix}'
    elif kind == 'run':
        command = 'sop-board run prepare --wait 540 --scope ' + ' '.join(scope)
    else:
        command = 'sop-board distill prepare ' + ' '.join(scope)
    if kind == 'run' and allow_open_fix:
        command += ' --allow-open-fix'
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
    def __init__(self, project, store, gate):
        self.project, self.store, self.gate = project, store, gate
        self.processes = {}
        self.relays = {}
        self.pending = set()
        self.closing = threading.Event()
        self.adopted = []   # 本进程启动时认回的启动（restart 交接或看板被强杀后仍在运行的会话）
        self.guard = threading.RLock()
        self.retry_delay = float(os.environ.get('SOP_BOARD_RETRY_DELAY', '60'))
        if not 0 <= self.retry_delay < float('inf'):
            raise ValueError('SOP_BOARD_RETRY_DELAY 须为非负有限秒数')

    def start(self, checkout, kind, scope, *, requested_by, implementer=None, fix=None, implemented_by=None,
              allow_open_fix=False):
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
            # 即将启动的是新的执行会话，不把发起者当作这轮的执行者。
            self.gate.check_open_fix(checkout, None, allow_open_fix=allow_open_fix)
        else:
            from .guards import validate_distill_scope
            validate_distill_scope(context, scope)
        agent = agent_for(self.project, kind)
        directory = self.project.data / 'launches'
        directory.mkdir(exist_ok=True)
        with self.guard:
            if self.closing.is_set():
                raise APIError(409, 'restarting', [dict(field='launch', problem='看板正在停止或交接，等本机接口重新就绪后再启动')])
            with self.store.local_tx() as db:
                running_launch(db, checkout)
                cursor = db.execute(
                    "INSERT INTO launches(checkout,kind,agent,scope,log,status,requested_by,started_at) VALUES (?,?,?,?,?,'running',?,?)",
                    (checkout.id, kind, agent['runner'], dumps(scope), '', requested_by, now()))
                launch_id = cursor.lastrowid
            from . import workdirs
            directory = workdirs.create(directory, launch_id)
            log = directory / 'output.jsonl'
            prompt = prompt_for(self.project, checkout, kind, scope, implementer, fix, implemented_by,
                                allow_open_fix=allow_open_fix)
            session = str(uuid.uuid4()) if agent['runner'] == 'claude' else None
            settings = dict(agent=agent, implementer=implementer, implemented_by=implemented_by)
            attempts = [dict(attempt=1, session=session, pid=None, log=str(log), started_at=now(), ended_at=None,
                             exit_code=None, settings=settings)] if kind == 'run' else []
            self.spawn_attempt(launch_id, checkout, settings, prompt, log, session, attempts)
        result = self.read(launch_id)
        result['invalid_files'] = excluded
        return result

    def spawn_attempt(self, launch_id, checkout, settings, prompt, log, session, attempts):
        """调用方持有 guard；先保存尝试，再启动进程，接续最多只能写出第二次尝试。"""
        agent = settings['agent']
        log.with_suffix('.prompt.md').write_text(prompt, encoding='utf-8')
        if attempts:
            write_attempts(log, attempts)
        with self.store.local_tx() as db:
            db.execute('UPDATE launches SET pid=NULL, log=?, session=?, exit_code=NULL, ended_at=NULL WHERE id=?',
                       (str(log), session, launch_id))
        env = dict(os.environ, SOP_BOARD_LAUNCH=str(launch_id), SOP_BOARD_CHECKOUT=checkout.name)
        for key in ('SOP_BOARD_SESSION', 'SOP_BOARD_IMPLEMENTER', 'SOP_BOARD_IMPLEMENTED_BY'):
            env.pop(key, None)
        if session:
            env['SOP_BOARD_SESSION'] = session
        if settings.get('implementer'):
            env['SOP_BOARD_IMPLEMENTER'] = settings['implementer']
        elif settings.get('implemented_by'):
            env['SOP_BOARD_IMPLEMENTED_BY'] = settings['implemented_by']
        work = work_directory(self.project, checkout)
        writable = [str(path.resolve()) for path in (self.project.data, self.project.data.parent / 'evidence')]
        argv = self.argv(agent, prompt, work, writable, session)
        try:
            process = self.spawn(argv, work, env, log)
        except OSError as error:
            message = redact(f"无法启动 {agent['runner']}：{error}".encode(), []).decode()
            log.write_text(message + '\n', encoding='utf-8')
            self.complete(launch_id, None, session, error=message)
            raise ValueError(message) from error
        if attempts:
            attempts[-1]['pid'] = process.pid
            write_attempts(log, attempts)
        with self.store.local_tx() as db:
            db.execute('UPDATE launches SET pid=? WHERE id=?', (process.pid, launch_id))
        self.processes[launch_id] = process
        threading.Thread(target=self.watch, args=(launch_id, process, log), daemon=True, name=f'launch-{launch_id}').start()

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
        self.relays[process.pid] = relay
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
        with self.store.local() as db:
            kind = db.execute('SELECT kind FROM launches WHERE id=?', (launch_id,)).fetchone()['kind']
        while process.poll() is None:
            if session is None:
                session = self.session_from_log(log)
                if session:
                    with self.guard:
                        with self.store.local_tx() as db:
                            db.execute('UPDATE launches SET session=COALESCE(session, ?) WHERE id=?', (session, launch_id))
                        with self.store.local() as db:
                            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                        attempts = read_attempts(row)
                        if attempts:
                            attempts[-1]['session'] = session
                            write_attempts(log, attempts)
            if kind == 'run':
                if self.closing.wait(1):
                    return
            else:
                time.sleep(1)
        if kind == 'run' and not adopted and process.returncode:
            # Claude 外层进程被杀时，结束同组里尚存的客户端，接续只留下一个执行会话。
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        relay = self.relays.pop(process.pid, None)
        if kind == 'run' and relay is not None:
            try:
                relay.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # 认回的会话不是本进程的子进程，取不到退出码：记 exited，有没有产出运行由 public() 判断（没有即 no-work）。
        code = None if adopted else process.returncode
        session = session or self.session_from_log(log)
        self.complete(launch_id, code, session)

    def complete(self, launch_id, code, session=None, *, error=None):
        """先落退出信息，再安排接续或收尾；共享库登记在另一个线程里。"""
        with self.guard:
            with self.store.local_tx() as db:
                row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                if row['kind'] == 'run' and self.closing.is_set():
                    return
                if row['kind'] == 'run' and not row['log']:
                    from .workdirs import create
                    log = create(self.project.data / 'launches', launch_id) / 'output.jsonl'
                    log.write_text((error or '启动在准备会话之前中断') + '\n', encoding='utf-8')
                    db.execute('UPDATE launches SET log=? WHERE id=?', (str(log), launch_id))
                    row = dict(row, log=str(log))
                ended = now()
                status = ('stopped' if row['status'] == 'stopped' else
                          'failed' if error or code is not None and code != 0 else 'exited')
                attempts = read_attempts(row)
                retry = (row['kind'] == 'run' and status == 'failed' and code is not None and len(attempts) == 1
                         and 'settings' in attempts[0])
                if row['kind'] == 'run' and not attempts:
                    attempts = [dict(attempt=1, session=row['session'], pid=row['pid'], log=row['log'],
                                     started_at=row['started_at'], ended_at=None, exit_code=None)]
                prepared = [r['id'] for r in db.execute(
                    "SELECT id FROM local_work WHERE launch=? AND kind='run' AND status='prepared'", (launch_id,))]
                cleanup = row['kind'] == 'run' and (status == 'failed' or status == 'exited' and prepared) and not retry
                if attempts:
                    attempts[-1].update(session=session or row['session'], ended_at=ended, exit_code=code)
                    if error:
                        attempts[-1]['error'] = error
                    if retry:
                        attempts[-1]['retry_at'] = (datetime.fromisoformat(ended) + timedelta(seconds=self.retry_delay)).isoformat()
                    elif cleanup:
                        attempts[-1]['cleanup'] = dict(runs=prepared, complete=False)
                    write_attempts(row['log'], attempts)
                db.execute('UPDATE launches SET status=?, exit_code=?, ended_at=?, session=COALESCE(?, session) WHERE id=?',
                           ('running' if retry else status, None if retry else code, None if retry else ended, session, launch_id))
            self.processes.pop(launch_id, None)
            if retry:
                self.pending.add(launch_id)
                threading.Thread(target=self.retry, args=(launch_id,), daemon=True, name=f'launch-retry-{launch_id}').start()
            elif cleanup:
                threading.Thread(target=self.cleanup, args=(launch_id,), daemon=True, name=f'launch-cleanup-{launch_id}').start()

    def retry(self, launch_id):
        with self.store.local() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
        attempts = read_attempts(row)
        deadline = datetime.fromisoformat(attempts[0]['retry_at']).timestamp()
        if self.closing.wait(max(0, deadline - time.time())):
            return
        if not self.wait_ready():
            return
        with self.guard:
            if self.closing.is_set():
                return
            with self.store.local() as db:
                row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            attempts = read_attempts(row)
            self.pending.discard(launch_id)
            if row['status'] != 'running' or len(attempts) != 1:
                return
            first = attempts[0]
            log = Path(first['log']).parent / 'output.2.jsonl'
            settings = first['settings']
            session = str(uuid.uuid4()) if settings['agent']['runner'] == 'claude' else None
            attempts.append(dict(attempt=2, session=session, pid=None, log=str(log), started_at=now(), ended_at=None, exit_code=None))
            write_attempts(log, attempts)
            with self.store.local_tx() as db:
                db.execute('UPDATE launches SET log=?, pid=NULL, session=? WHERE id=?', (str(log), session, launch_id))
            try:
                from .checkouts import by_id
                with self.store.local() as db:
                    checkout = by_id(db, row['checkout'])
                    runs = [dict(r) for r in db.execute("SELECT id, dir, status FROM local_work WHERE launch=? AND kind='run' ORDER BY id",
                                                        (launch_id,))]
                previous = Path(first['log']).with_suffix('.prompt.md').read_text(encoding='utf-8')
                resumed = Template(files('sop_board').joinpath('prompts', 'launch-retry.md').read_text(encoding='utf-8')).substitute(
                    ended_at=first['ended_at'], exit_code=first['exit_code'], log=first['log'],
                    runs='\n'.join(f"- 运行 #{r['id']}，目录 {r['dir']}，状态 {r['status']}。" for r in runs) or '没有准备运行。')
                self.spawn_attempt(launch_id, checkout, settings, previous + '\n' + resumed, log, session, attempts)
            except Exception as error:  # 接续准备或进程创建失败都结束这次启动。
                message = '接续没能发起：' + error_text(error)
                print(f'启动 #{launch_id}：{message}', flush=True)
                with self.store.local() as db:
                    current = db.execute('SELECT status FROM launches WHERE id=?', (launch_id,)).fetchone()
                if current['status'] == 'running':
                    log.write_text(message + '\n', encoding='utf-8')
                    self.complete(launch_id, None, session, error=message)

    def cleanup(self, launch_id):
        """全部释放在前，可能等待共享库的登记在后；进程重启可按磁盘标记继续。"""
        try:
            if self.wait_ready():
                self._cleanup(launch_id)
        except Exception as error:
            message = '收尾未完成：' + error_text(error)
            print(f'启动 #{launch_id}：{message}', flush=True)
            # 把原因写进尝试记录：run wait 据此返回并写明原因；收尾标记不变，看板下次启动时接着做。
            with self.guard:
                with self.store.local() as db:
                    row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                attempts = read_attempts(row)
                if finishing(attempts):
                    attempts[-1]['cleanup']['problem'] = message
                    write_attempts(row['log'], attempts)

    def wait_ready(self):
        # recover 在发布本机接口之前运行；到期接续和服务恢复都等本次守护进程就绪。
        from .daemon import describe
        while not self.closing.is_set():
            if describe(self.project).get('pid') == os.getpid():
                return True
            self.closing.wait(.1)
        return False

    def _cleanup(self, launch_id):
        from . import runs
        from .checkouts import by_id
        from .cli import Context
        from .ledger import insert_attention
        with self.store.local() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            checkout = by_id(db, row['checkout'])
        attempts = read_attempts(row)
        if row['status'] == 'stopped' or not finishing(attempts):
            return
        cleanup = attempts[-1]['cleanup']
        context = Context(self.project, self.store, checkout)
        cleanup.pop('problem', None)
        for number in list(cleanup['runs']):
            if self.closing.is_set():
                return
            try:
                runs.abandon(context, number, by='sop-board', launch=launch_id)
            except APIError as error:
                if error.code != 'run-not-prepared':
                    raise
                cleanup['runs'].remove(number)   # 已提交，或已由别人放弃：不归这次收尾处理
        if row['status'] == 'exited' and not cleanup['runs']:
            # 收尾开始前运行已提交（或已由别人放弃）：这次启动没有要处理的运行，不登记待人处理。
            cleanup['complete'] = True
            with self.guard:
                write_attempts(row['log'], attempts)
            return
        # 日志已遮蔽，再按当前遮蔽规则处理一次；固定末尾行，重启后登记的文字保持相同。
        last = next(iter(reversed(log_tail(row['log']).splitlines())), '（日志为空）')
        cleanup.setdefault('last_line', last if len(last) <= LAST_LINE_LIMIT else last[:LAST_LINE_LIMIT] + '…')
        cleanup.setdefault('attentions', [])
        with self.guard:
            write_attempts(row['log'], attempts)
        if not cleanup['attentions']:
            if self.closing.is_set():
                return
            number = cleanup['runs'][0] if cleanup['runs'] else None
            names = '、'.join(f'#{number}' for number in cleanup['runs'])
            codes = '、'.join(str(a.get('exit_code')) for a in attempts)
            if row['status'] == 'exited':
                reason = '的执行会话正常退出，但没有提交运行'
            elif attempts[-1].get('error'):
                reason = f"失败：{attempts[-1]['error']}"
            elif len(attempts) > 1:
                reason = f'的执行会话接续一次后仍异常退出（退出码 {codes}）'
            else:
                reason = f'的执行会话异常退出（退出码 {codes}），没有接续'
            text = (f'启动 #{launch_id} {reason}；' + (f'运行 {names} 已由看板放弃；' if names else '没有准备运行；')
                    + f"涉及场景：{'、'.join(json.loads(row['scope']))}；日志末尾一行：{cleanup['last_line']}"
                    + f'；完整日志在产出机器上用 sop-board launch status 查看')
            # 待人处理是共享的业务记录：日志末尾一行里的本机路径一并遮蔽。
            text = hide_local_paths(text.encode(), [self.project.data, *checkout.paths.values()]).decode()
            item = dict(run=number)
            try:
                with self.store.records_tx() as db:
                    # 重启发生在登记之后、保存编号之前时，返回已登记的同一条。
                    clause = 'work IS NULL' if number is None else 'work=?'
                    parameters = (self.store.product, self.store.machine['id'], checkout.name, text) + (() if number is None else (number,))
                    existing = db.one("SELECT id FROM attention WHERE product=? AND machine=? AND checkout=? AND text=? "
                                      f"AND created_by='sop-board' AND kind='环境问题' AND {clause}", parameters)
                    item['attention'] = existing['id'] if existing else insert_attention(
                        db, self.store, checkout.name, '环境问题', text, by='sop-board',
                        scenarios=json.loads(row['scope']), work=number)
            except Exception as error:
                item['problem'] = error_text(error)
                print(f"启动 #{launch_id}：待人处理没登记上：{item['problem']}", flush=True)
            cleanup['attentions'].append(item)
            with self.guard:
                write_attempts(row['log'], attempts)
        item = cleanup['attentions'][0]
        for number in cleanup['runs']:
            if self.closing.is_set():
                return
            with self.store.local_tx() as db:
                work = db.execute('SELECT result FROM local_work WHERE id=?', (number,)).fetchone()
                result = json.loads(work['result'] or '{}')
                if 'attention' in item:
                    result['launch_attention'] = item['attention']
                else:
                    result['launch_attention_problem'] = item['problem']
                db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(result), number))
        cleanup['complete'] = True
        with self.guard:
            write_attempts(row['log'], attempts)

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
        with self.guard:
            with self.store.local_tx() as db:
                row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                if row is None:
                    raise APIError(404, 'launch-not-found', [dict(field='launch', problem=f'启动不存在：#{launch_id}')])
                if row['status'] != 'running':
                    return self.read(launch_id)
                db.execute("UPDATE launches SET status='stopped' WHERE id=?", (launch_id,))
                if row['kind'] == 'run':
                    db.execute('UPDATE launches SET ended_at=? WHERE id=?', (now(), launch_id))
            self.pending.discard(launch_id)
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
            return state(db, row, local=local)[0] if row else None

    def recover(self):
        """认回会话、恢复已到期的接续，以及中断在退出之后的收尾。"""
        with self.store.local() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM launches WHERE status IN ('running','failed','exited')")]
        adopted = []
        for row in rows:
            attempts = read_attempts(row)
            if row['status'] != 'running':
                if finishing(attempts):
                    threading.Thread(target=self.cleanup, args=(row['id'],), daemon=True, name=f"launch-cleanup-{row['id']}").start()
                continue
            if row['kind'] == 'run' and len(attempts) == 1 and attempts[0].get('retry_at'):
                self.pending.add(row['id'])
                threading.Thread(target=self.retry, args=(row['id'],), daemon=True, name=f"launch-retry-{row['id']}").start()
                adopted.append(dict(launch=row['id'], pid=None))
                print(f"已恢复启动 #{row['id']} 的接续等待", flush=True)
                continue
            # 第二次尝试可以比启动记录晚几分钟；进程身份按这次尝试的时间核对。
            current = dict(row, started_at=attempts[-1]['started_at']) if attempts else row
            process = adoptable(current)
            if process is None:
                self.complete(row['id'], None, error='重启后没有找到本次尝试的会话进程')
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
                    if r['id'] in self.processes or r['id'] in self.pending]

    def shutdown(self, *, handover=False):
        """正常停止时结束全部会话；restart 交接时会话照常运行，由新守护进程认回。"""
        if handover:
            with self.guard:
                self.closing.set()
            kept = self.running()
            for item in kept:
                print(f"已保留启动 #{item['launch']}：会话进程 {item['pid']}", flush=True)
            return kept
        for launch_id in set(self.processes) | self.pending:
            self.stop(launch_id)
        with self.guard:
            self.closing.set()
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


def public(row, runs, *, local, attempts=None):
    attempts = read_attempts(row) if attempts is None else attempts
    item = dict(id=row['id'], kind=row['kind'], agent=row['agent'], scope=json.loads(row['scope']), status=row['status'],
                exit_code=row['exit_code'], requested_by=row['requested_by'], started_at=row['started_at'],
                ended_at=row['ended_at'], checkout=row['checkout'], runs=[dict(id=r['id'], kind=r['kind'], status=r['status']) for r in runs])
    if row['kind'] == 'run':
        fields = ('attempt', 'session', 'pid', 'log', 'started_at', 'ended_at', 'exit_code', 'retry_at', 'error') if local else (
            'attempt', 'started_at', 'ended_at', 'exit_code', 'retry_at')
        item['attempts'] = [{key: attempt[key] for key in fields if key in attempt} for attempt in attempts]
        if finishing(attempts) and not attempts[-1]['cleanup'].get('problem'):
            item['finishing'] = True
    if row['status'] == 'exited' and not runs:
        item.update(status='no-work', problem=f"启动 #{row['id']} 未产生运行（或沉淀）")
    elif row['status'] == 'failed':
        item['problem'] = f"启动 #{row['id']} 失败" + ('' if runs else '，未产生运行（或沉淀）')
    if row['status'] in ('exited', 'failed'):
        unsubmitted = []
        for run in runs:
            if run['status'] not in ('prepared', 'abandoned'):
                continue
            summary = json.loads(run.get('result') or '{}')
            entry = dict(id=run['id'], kind=run['kind'], status=run['status'])
            if summary.get('abandoned_by') == 'sop-board' and summary.get('launch') == row['id']:
                entry['abandoned_by'] = 'sop-board'
                if summary.get('launch_attention'):
                    entry['attention'] = summary['launch_attention']
                elif summary.get('launch_attention_problem'):
                    entry['attention_problem'] = summary['launch_attention_problem']
            unsubmitted.append(entry)
        if unsubmitted:
            item['unsubmitted'] = unsubmitted
            details = []
            for run in unsubmitted:
                text = f"{'运行' if run['kind'] == 'run' else '沉淀'} #{run['id']} "
                if run.get('abandoned_by') == 'sop-board':
                    text += '未提交，已由看板放弃（abandoned，未登记结果）'
                    text += (f"，待人处理 #{run['attention']}" if run.get('attention') else
                             f"，待人处理没登记上：{run['attention_problem']}" if run.get('attention_problem') else '，待人处理登记中')
                else:
                    text += '未提交（prepared）' if run['status'] == 'prepared' else '已放弃（abandoned，未登记结果）'
                details.append(text)
            item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + '；' + '、'.join(details)
        elif attempts:
            for attention in attempts[-1].get('cleanup', {}).get('attentions', []):
                item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + (
                    f"；待人处理 #{attention['attention']}" if attention.get('attention') else f"；待人处理没登记上：{attention['problem']}")
    stuck = attempts[-1].get('cleanup', {}).get('problem') if attempts else None
    if stuck:
        item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + '；' + stuck
    if item['status'] in ('no-work', 'failed') or item.get('unsubmitted'):
        if local:
            item['log_tail'] = log_tail(row['log'])
    if local:
        item.update(pid=row['pid'], log=row['log'], session=row['session'],
                    prompt=str(Path(row['log']).with_suffix('.prompt.md')) if row['log'] else None)
    return item


# ---- 守护进程挂载与接口 ----

def attach(app):
    from . import extensions
    app.launcher = Launcher(app.project, app.store, app.gate)
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
            implemented_by=request.json_field('implemented_by'), fix=request.json_field('fix'),
            allow_open_fix=bool(request.json_field('allow_open_fix')))))

    @route('POST', '/local/launch/stop', 'token')
    def stop(app, request):
        return json_response(dict(ok=True, launch=app.launcher.stop(int(request.json_field('launch')))))


_register()
