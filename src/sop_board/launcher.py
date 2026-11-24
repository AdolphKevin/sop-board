"""无头会话启动器（守护进程侧）：用同一份 sop-board 技能在检出目录启动 claude -p 或 codex exec。

会话经 CLI 准备、逐条裁决与提交，同一检出同时只运行一个启动。取得进展且仍有未裁决时接续，
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

from . import progress
from .config import EFFORTS
from .errors import APIError
from .redaction import business_text, redact
from .store import dumps, now

KINDS = ('run', 'distill')
NOTE_FILE = 'note.md'
NOTE_LIMIT = 4000
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
    progress.atomic_json(path, attempts)


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


def state(db, row, *, local, store=None):
    # 先读完成标记，再读运行：完成标记出现时，运行的放弃与登记编号已经写好。
    attempts = read_attempts(row)
    runs = [dict(r) for r in db.execute('SELECT * FROM local_work WHERE launch=? ORDER BY id', (row['id'],))]
    return public(row, runs, local=local, attempts=attempts, store=store), runs


def group_has_work(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        listing = subprocess.run(['ps', '-axo', 'pgid=,stat='], text=True, capture_output=True,
                                 timeout=2, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return any(group == str(pid) and not state.startswith('Z')
               for group, state in (line.split() for line in listing.splitlines() if len(line.split()) == 2))


def drain_group(pid, *, owned=False):
    if not pid:
        return True
    if not owned:
        from .browsers import process_snapshot
        if process_snapshot(pid) is not None:
            # 旧会话已不在，存在同号的新进程时不能向它发信号。
            return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_has_work(pid):
            return True
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not group_has_work(pid):
                return True
            time.sleep(.1)
    return not group_has_work(pid)


def archive_drafts(work, attempt):
    from .runs import _strict_json, resolve_key, row_hash
    directory = Path(work['dir'])
    path = directory / 'results.jsonl'
    with progress.lock(work):
        if not path.is_file() or path.is_symlink():
            return
        data = progress.read(work)
        scope = progress.scope(work)
        kept, archived, seen = [], [], set()
        for raw in path.read_bytes().splitlines(keepends=True):
            if not raw.strip():
                kept.append(raw)
                continue
            try:
                row = _strict_json(raw)
            except ValueError:
                row = None
            key = resolve_key(scope, row.get('scenario')) if isinstance(row, dict) else None
            entry = data['adjudications'].get(key)
            if key in seen or not entry or entry.get('row_sha256') != row_hash(row):
                archived.append(raw)
            else:
                kept.append(raw)
                seen.add(key)
        if archived:
            archive = directory / f'results.attempt.{attempt}.draft.jsonl'
            if not archive.exists():
                progress.atomic_bytes(archive, b''.join(archived))
            progress.atomic_bytes(path, b''.join(kept))


def continuation_notes(runs):
    lines = []
    for work in runs:
        data = progress.read(work)
        lines.append(f"- 运行 #{work['id']}，目录 {work['dir']}。")
        registered = ['%s (%s)' % (key, entry['status']) for key, entry in sorted(data['adjudications'].items())
                      if entry['registration'] == 'registered']
        lines.append('  - 已登记：' + ('、'.join(registered) or '无'))
        local = progress.local_verdicts(data)
        lines.append('  - 本机暂记：' + ('、'.join(
            f"{row['scenario']}（{row['status']} / {row['cause'] or '无类别'}，{row['finding'] or ''}）" for row in local) or '无'))
        lines.append('  - 未裁决：' + ('、'.join(progress.unadjudicated(work, data)) or '无'))
        for item in progress.in_progress(data):
            lines.append(f"  - 已开始尚未结束：{item['scenario']}，开始于 {item['started_at']}。先核对并还原数据。")
    return '\n'.join(lines) or '- 上一个会话没有准备运行。'


def runner_problem(runner, model, effort, *, kind):
    if runner is None and model is None and effort is None:
        return None
    if kind != 'launch':
        return dict(field='runner', problem='--runner、--model 和 --effort 只用于 run launch')
    if runner not in ('codex', 'claude'):
        return dict(field='runner', problem='须用 --runner codex 或 --runner claude')
    if model is not None and (not isinstance(model, str) or len(model.strip().splitlines()) != 1 or '\x00' in model):
        return dict(field='model', problem='--model 须为非空单行文字')
    if effort is not None and effort not in EFFORTS:
        return dict(field='effort', problem='--effort 应为 ' + '、'.join(EFFORTS))
    return None


def agent_args(runner, args):
    """提取客户端的模型与强度参数，用各客户端的专用选项统一传递。"""
    remaining, preferences = [], {}
    fields = {'--model': 'model', '--effort': 'effort'} if runner == 'claude' else {'--model': 'model', '-m': 'model'}
    i = 0
    while i < len(args):
        arg = args[i]
        option, equals, value = arg.partition('=')
        if runner == 'codex' and option.startswith(('-m', '-c')) and len(option) > 2:
            option, equals, value = arg[:2], '=', arg[2:].removeprefix('=')
        size = 1
        if not equals and i + 1 < len(args):
            value, size = args[i + 1], 2
        field = fields.get(option)
        if runner == 'codex' and option in ('-c', '--config'):
            name, _, raw = value.partition('=')
            field = {'model': 'model', 'model_reasoning_effort': 'effort'}.get(name.strip())
            if field:
                value = raw.strip()
                if value.startswith('"') and value.endswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
        if field and (equals or size == 2):
            preferences[field] = value
            i += size
        else:
            remaining.append(arg)
            i += 1
    return remaining, preferences


def agent_for(project, kind, *, runner=None, model=None, effort=None):
    configured = project.agents.get(kind) or project.agents.get('default') or dict(runner='codex')
    runner = runner or configured['runner']
    if runner != configured['runner']:
        configured = {}
    args, preferences = agent_args(runner, configured.get('args') or [])
    if runner == 'claude' and not configured.get('args'):
        args = ['--permission-mode', 'acceptEdits', '--allowedTools', 'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep']
    return dict(runner=runner, args=args,
                model=model.strip() if model is not None else configured.get('model', preferences.get('model')),
                effort=effort if effort is not None else configured.get('effort', preferences.get('effort')))


def new_attempt(number, agent, session, log):
    return dict(attempt=number, session=session, pid=None, log=str(log), started_at=now(), ended_at=None, exit_code=None,
                runner=agent['runner'], model=agent.get('model'), effort=agent.get('effort'))


def work_directory(project, checkout):
    return checkout.paths[project.scenario_repo].resolve()


def prompt_for(project, checkout, kind, scope, implementer, fix=None, implemented_by=None, *, allow_open_fix=False,
               preflight=None, with_identities=None):
    if fix is not None:
        command = f'sop-board run prepare --wait 540 --fix {fix}'
    elif kind == 'run':
        command = 'sop-board run prepare --wait 540 --scope ' + ' '.join(scope)
    else:
        command = 'sop-board distill prepare ' + ' '.join(scope)
    if kind == 'run' and allow_open_fix:
        command += ' --allow-open-fix'
    if kind == 'run':
        command += ''.join(f' --with-identity {identity}' for identity in with_identities or ())
    run_workflow = '按 prompt.md 把场景分组，一次开始一组，留证后逐条裁决。' if kind == 'run' else ''
    text = Template(files('sop_board').joinpath('prompts', 'launch.md').read_text(encoding='utf-8')).substitute(
        project=project.name, checkout=checkout.name, directory=work_directory(project, checkout),
        kind='场景执行' if kind == 'run' else '回归沉淀', scope='、'.join(scope), command=command, run_workflow=run_workflow,
        implementer=f'首次裁决或提交时加 --implementer {implementer}。' if implementer
        else f'首次裁决或提交时加 --implemented-by {implemented_by}（实现者是别的操作者，跨机器验收）。' if implemented_by else '')
    if preflight and preflight['probe_skipped']:
        text += '\n## 启动时的入口预检\n\n'
        text += '按原范围准备，准备会重新探测并决定实际范围。启动时以下场景所用入口不健康：\n\n'
        text += ''.join(f"- {item['scenario']}：{item['finding']}\n" for item in preflight['probe_skipped'])
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
        delay = os.environ.get('SOP_BOARD_RETRY_DELAY', '60')
        if not (delay.isascii() and delay.isdecimal()):
            raise ValueError('SOP_BOARD_RETRY_DELAY 须为非负整数秒')
        self.retry_delay = int(delay)

    def start(self, checkout, kind, scope, *, requested_by, implementer=None, fix=None, implemented_by=None,
              allow_open_fix=False, runner=None, model=None, effort=None, note=None, with_identities=None):
        if kind not in KINDS:
            raise ValueError('启动类型应为 run 或 distill')
        if note is not None:
            # 本轮说明：只对这一次启动生效，写进它的运行的 brief（已定决策 31 第五段）。
            if kind != 'run' or not isinstance(note, str) or not note.strip() or len(note) > NOTE_LIMIT:
                raise APIError(422, 'invalid-arguments', [dict(field='note', problem=f'本轮说明只用于 run launch，须为不超过 {NOTE_LIMIT} 字的非空文字')])
            note = note.strip()
        if not scope:
            raise ValueError('须给出场景')
        problem = runner_problem(runner, model, effort, kind='launch' if kind == 'run' else kind)
        if problem:
            raise APIError(422, 'invalid-arguments', [problem])
        self.project.require()
        if with_identities:
            from .runs import extra_identities
            if kind != 'run' or not isinstance(with_identities, list) or any(not isinstance(item, str) for item in with_identities):
                raise APIError(422, 'invalid-arguments', [dict(field='with-identity', problem='另外取得的身份只用于 run launch，须为身份编号')])
            extra_identities(self.project, with_identities)
        # 已有运行中的启动时先报 launch-running：它可能已准备了运行，范围检查会把那些场景当成别的运行占着的。
        with self.store.local() as db:
            running_launch(db, checkout)
        from .cli import Context
        context = Context(self.project, self.store, checkout)
        excluded = []
        selected_dispositions = []
        held = []
        preflight = None
        if kind == 'run':
            from .runs import validate_scope, invalid_files, probe_candidates, selected_holds
            from . import dispositions, fixes, probes
            verification = fixes.for_verify(context, fix, None) if fix is not None else None
            found, _, derived, selected = validate_scope(context, scope, verification)
            excluded = invalid_files(found)
            scope = [item['scenario'] for item in selected]
            selected_dispositions = dispositions.listed(derived['items'], scope)
            held = selected_holds(derived, scope)
            # 即将启动的是新的执行会话，不把发起者当作这轮的执行者。
            self.gate.check_open_fix(checkout, None, allow_open_fix=allow_open_fix)
            candidates = probe_candidates(selected, found['scenarios'])
            reports = probes.batch(self.gate.app.envs, checkout, [item['entry'] for item in candidates], ready_only=True)
            preflight = dict(probes=reports, probe_skipped=probes.excluded(candidates, reports, fixing=fix is not None))
        else:
            from .guards import validate_distill_scope
            validate_distill_scope(context, scope)
        agent = agent_for(self.project, kind, runner=runner, model=model, effort=effort)
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
            if preflight is not None:
                (directory / 'preflight.json').write_text(dumps(preflight) + '\n', encoding='utf-8')
            if note is not None:
                (directory / NOTE_FILE).write_text(note + '\n', encoding='utf-8')
            prompt = prompt_for(self.project, checkout, kind, scope, implementer, fix, implemented_by,
                                allow_open_fix=allow_open_fix, preflight=preflight, with_identities=with_identities)
            session = str(uuid.uuid4()) if agent['runner'] == 'claude' else None
            settings = dict(agent=agent, implementer=implementer, implemented_by=implemented_by)
            attempts = [dict(new_attempt(1, agent, session, log), settings=settings)] if kind == 'run' else []
            self.spawn_attempt(launch_id, checkout, settings, prompt, log, session, attempts)
        result = self.read(launch_id)
        result['invalid_files'] = excluded
        if kind == 'run':
            result['dispositions'] = selected_dispositions
            result['held'] = held
            result.update(preflight)
        return result

    def spawn_attempt(self, launch_id, checkout, settings, prompt, log, session, attempts):
        """调用方持有 guard；先保存尝试，再启动进程。"""
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
            if agent.get('model') is not None:
                argv += ['--model=' + agent['model']]
            if agent.get('effort') is not None:
                argv += ['--effort=' + agent['effort']]
            for path in writable:
                argv += ['--add-dir', path]
            argv += [prompt, '--', *agent.get('args', ())]
            return argv
        argv = ['codex', 'exec', '--json', '-s', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true',
                '--skip-git-repo-check', '-C', str(work)]
        for path in writable:
            argv += ['--add-dir', path]
        argv += agent.get('args', ())
        if agent.get('model') is not None:
            argv.append('--model=' + agent['model'])
        if agent.get('effort') is not None:
            argv += ['-c', 'model_reasoning_effort=' + json.dumps(agent['effort'])]
        return argv + ['--', prompt]

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
        if kind == 'run':
            drained = drain_group(process.pid, owned=not adopted)
            with self.guard:
                with self.store.local() as db:
                    finished = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                attempts = read_attempts(finished)
                if attempts:
                    attempts[-1]['group_drained'] = drained
                    write_attempts(log, attempts)
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

    def run_rows(self, launch_id, *, prepared=False):
        condition = " AND status='prepared' AND dir<>''" if prepared else ''
        with self.store.local() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM local_work WHERE launch=? AND kind='run'" + condition + ' ORDER BY id', (launch_id,))]

    def verdicts_after_exit(self, runs, session):
        all_keys, prepared_keys, remaining = set(), set(), False
        for work in runs:
            if not work['dir']:
                continue
            with progress.lock(work):
                with self.store.local() as db:
                    latest = db.execute('SELECT * FROM local_work WHERE id=?', (work['id'],)).fetchone()
                if not latest:
                    continue
                work = dict(latest)
                data = progress.recover(self.store, work) if work['status'] == 'prepared' else progress.read(work)
                new = data.get('sessions', {}).get(session, [])
                all_keys.update(new)
                if work['status'] == 'prepared' and not data.get('ended_at'):
                    prepared_keys.update(new)
                    remaining |= bool(progress.unadjudicated(work, data))
        return len(all_keys), len(prepared_keys), remaining

    def complete(self, launch_id, code, session=None, *, error=None):
        """先落退出信息，再按真实裁决判断接续或收尾。"""
        with self.guard:
            with self.store.local() as db:
                row = dict(db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone())
            if row['kind'] == 'run' and self.closing.is_set():
                return
            if row['kind'] == 'run' and not row['log']:
                from .workdirs import create
                log = create(self.project.data / 'launches', launch_id) / 'output.jsonl'
                log.write_text((error or '启动在准备会话之前中断') + '\n', encoding='utf-8')
                with self.store.local_tx() as db:
                    db.execute('UPDATE launches SET log=? WHERE id=?', (str(log), launch_id))
                row['log'] = str(log)
            attempts = read_attempts(row)
            if row['kind'] == 'run' and not attempts:
                attempts = [dict(attempt=1, session=row['session'], pid=row['pid'], log=row['log'],
                                 started_at=row['started_at'], ended_at=None, exit_code=None)]
            ended = (attempts[-1].get('ended_at') if attempts else None) or now()
            session = session or row['session'] or (attempts[-1].get('session') if attempts else None)
            terminal = ('stopped' if row['status'] == 'stopped' else
                        'failed' if error or code is not None and code != 0 else 'exited')
            prepared = self.run_rows(launch_id, prepared=True) if attempts else []
            retry, cleanup = False, False
            if attempts:
                current = attempts[-1]
                current.update(session=session, ended_at=ended, exit_code=code)
                if error:
                    current['error'] = error
                all_runs = self.run_rows(launch_id)
                try:
                    if terminal == 'stopped':
                        count = sum(len(progress.read(r).get('sessions', {}).get(session, [])) for r in all_runs)
                        prepared_count, remaining = 0, False
                    else:
                        count, prepared_count, remaining = self.verdicts_after_exit(all_runs, session)
                except (APIError, OSError, ValueError) as problem:
                    if not error:
                        current['decision_problem'] = error_text(problem)
                        write_attempts(row['log'], attempts)
                        self.processes.pop(launch_id, None)
                        if launch_id not in self.pending:
                            self.pending.add(launch_id)
                            threading.Thread(target=self.recheck_exit, args=(launch_id,), daemon=True,
                                             name=f'launch-check-{launch_id}').start()
                        return
                    count, prepared_count, remaining = 0, 0, False
                current.pop('decision_problem', None)
                current['new_verdicts'] = count
                started = sum(a.get('pid') is not None for a in attempts)
                # 首个会话在取得任何裁决之前异常退出（例如模型接口被拒），仍接续一次（已定决策 29 对瞬时故障的保护）。
                first_crash = (terminal == 'failed' and started == 1 and not count
                               and not any(r['status'] == 'submitted' for r in all_runs))
                retry = (terminal != 'stopped' and not error and started < 3 and bool(attempts[0].get('settings'))
                         and (first_crash or (bool(prepared_count) and remaining)))
                cleanup = terminal != 'stopped' and not retry and (terminal == 'failed' or bool(prepared))
                if retry:
                    current['retry_at'] = (datetime.fromisoformat(ended) + timedelta(seconds=self.retry_delay)).isoformat()
                elif cleanup:
                    reason = ('达到续跑上限' if started >= 3 and remaining else
                              '所有场景已裁决，运行尚未提交' if prepared and not remaining else
                              '没有新的裁决' if prepared and not prepared_count else '')
                    current['cleanup'] = dict(runs=[r['id'] for r in prepared], complete=False, reason=reason)
                write_attempts(row['log'], attempts)
            with self.store.local_tx() as db:
                db.execute('UPDATE launches SET status=?, exit_code=?, ended_at=?, session=COALESCE(?, session) WHERE id=?',
                           ('running' if retry else terminal, None if retry else code, None if retry else ended, session, launch_id))
            self.processes.pop(launch_id, None)
            self.pending.discard(launch_id)
            if retry:
                self.start_retry(launch_id)
            elif cleanup:
                threading.Thread(target=self.cleanup, args=(launch_id,), daemon=True, name=f'launch-cleanup-{launch_id}').start()

    def recheck_exit(self, launch_id):
        while not self.closing.wait(2):
            with self.guard:
                with self.store.local() as db:
                    row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
                if row['status'] != 'running':
                    self.pending.discard(launch_id)
                    return
                attempt = read_attempts(row)[-1]
                if not attempt.get('decision_problem'):
                    return
                self.complete(launch_id, attempt['exit_code'], attempt['session'])
                if not read_attempts(row)[-1].get('decision_problem'):
                    return

    def start_retry(self, launch_id):
        self.pending.add(launch_id)
        threading.Thread(target=self.retry, args=(launch_id,), daemon=True, name=f'launch-retry-{launch_id}').start()

    def retry(self, launch_id):
        with self.store.local() as db:
            row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
        attempts = read_attempts(row)
        deadline = datetime.fromisoformat(attempts[-1]['retry_at']).timestamp()
        if self.closing.wait(max(0, deadline - time.time())) or not self.wait_ready():
            return
        with self.guard:
            if self.closing.is_set():
                return
            with self.store.local() as db:
                row = db.execute('SELECT * FROM launches WHERE id=?', (launch_id,)).fetchone()
            attempts = read_attempts(row)
            self.pending.discard(launch_id)
            if row['status'] != 'running' or not attempts[-1].get('retry_at'):
                return
            first, last = attempts[0], attempts[-1]
            number = len(attempts) + 1
            log = Path(first['log']).parent / f'output.{number}.jsonl'
            settings = first['settings']
            session = str(uuid.uuid4()) if settings['agent']['runner'] == 'claude' else None
            try:
                if not last.get('group_drained') and not drain_group(last.get('pid')):
                    raise ValueError('上一会话进程组仍有进程，未发起接续')
                from .checkouts import by_id
                with self.store.local() as db:
                    checkout = by_id(db, row['checkout'])
                runs = self.run_rows(launch_id, prepared=True)
                for work in runs:
                    archive_drafts(work, last['attempt'])
                previous = Path(first['log']).with_suffix('.prompt.md').read_text(encoding='utf-8')
                resumed = Template(files('sop_board').joinpath('prompts', 'launch-retry.md').read_text(encoding='utf-8')).substitute(
                    ended_at=last['ended_at'], exit_code=last['exit_code'] if last['exit_code'] is not None else '未知',
                    log=last['log'], runs=continuation_notes(runs))
                attempts.append(new_attempt(number, settings['agent'], session, log))
                self.spawn_attempt(launch_id, checkout, settings, previous + '\n' + resumed, log, session, attempts)
            except Exception as error:  # 接续准备或进程创建失败都结束这次启动。
                message = '接续没能发起：' + error_text(error)
                print(f'启动 #{launch_id}：{message}', flush=True)
                with self.store.local() as db:
                    current = db.execute('SELECT status FROM launches WHERE id=?', (launch_id,)).fetchone()
                if current['status'] == 'running':
                    self.complete(launch_id, None, last['session'], error=message)

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
        summaries = {}
        for number in list(cleanup['runs']):
            if self.closing.is_set():
                return
            try:
                summaries[number] = runs.abandon(context, number, by='sop-board', launch=launch_id)
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
        cleanup.setdefault('last_line', last if len(last) <= LAST_LINE_LIMIT else last[:LAST_LINE_LIMIT - 1] + '…')
        cleanup.setdefault('attentions', [])
        with self.guard:
            write_attempts(row['log'], attempts)
        if not cleanup['attentions']:
            if self.closing.is_set():
                return
            number = cleanup['runs'][0] if cleanup['runs'] else None
            names = '、'.join(f'#{number}' for number in cleanup['runs'])
            codes = '、'.join(str(a['exit_code']) if a.get('exit_code') is not None else '未知' for a in attempts)
            if row['status'] == 'exited':
                reason = '的执行会话正常退出，但没有提交运行'
            elif attempts[-1].get('error'):
                reason = f"失败：{attempts[-1]['error']}"
            else:
                reason = '的执行会话异常退出'
            if cleanup.get('reason'):
                reason += f"（{cleanup['reason']}）"
            scoped = {r['id']: sorted(progress.scope(r)) for r in self.run_rows(launch_id) if r['id'] in summaries}
            scenarios = sorted({key for keys in scoped.values() for key in keys}) or json.loads(row['scope'])
            details = '；'.join(f"运行 #{n}：{'、'.join(keys)}" for n, keys in scoped.items())
            local_notes = []
            for n, result in summaries.items():
                for verdict in result['unregistered']:
                    local_notes.append(f"运行 #{n} 的 {verdict['scenario']}：{verdict['status']}，"
                                       f"{verdict['cause'] or ''}，{verdict['finding'] or ''}")
            text = (f'启动 #{launch_id} {reason}，各次退出码 {codes}；'
                    + (f'运行 {names} 已由看板放弃；{details}；' if names else '没有准备运行；')
                    + f"涉及场景：{'、'.join(scenarios)}；日志末尾一行：{cleanup['last_line']}")
            if local_notes:
                text += '；本机暂记：' + '；'.join(local_notes)
            text += '；完整日志在产出机器上用 sop-board launch status 查看'
            text = cleanup.setdefault('text', business_text(self.project, checkout, text))
            with self.guard:
                write_attempts(row['log'], attempts)
            item = dict(run=number)
            try:
                with self.store.records_tx() as db:
                    db.lock_sequence()
                    # 重启发生在登记之后、保存编号之前时，返回已登记的同一条。
                    clause = 'work IS NULL' if number is None else 'work=?'
                    parameters = (self.store.product, self.store.machine['id'], checkout.name, text) + (() if number is None else (number,))
                    existing = db.one("SELECT id FROM attention WHERE product=? AND machine=? AND checkout=? AND text=? "
                                      f"AND created_by='sop-board' AND kind='环境问题' AND {clause}", parameters)
                    item['attention'] = existing['id'] if existing else insert_attention(
                        db, self.store, checkout.name, '环境问题', text, by='sop-board',
                        scenarios=scenarios, work=number)
            except Exception as error:
                message = error_text(error)
                for n in cleanup['runs']:
                    with self.store.local_tx() as db:
                        saved = db.execute('SELECT result FROM local_work WHERE id=?', (n,)).fetchone()
                        result = json.loads(saved['result'] or '{}')
                        result['launch_attention_problem'] = message
                        db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(result), n))
                raise
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
                    result.pop('launch_attention_problem', None)
                else:
                    result['launch_attention_problem'] = item['problem']
                db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(result), number))
        problems = [r['records_problem'] for r in summaries.values() if r.get('records_problem')]
        if problems:
            raise RuntimeError('；'.join(problems))
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
                running = row['status'] == 'running'
                if running:
                    db.execute("UPDATE launches SET status='stopped' WHERE id=?", (launch_id,))
                    if row['kind'] == 'run':
                        db.execute('UPDATE launches SET ended_at=? WHERE id=?', (now(), launch_id))
            if not running:
                return self.read(launch_id)
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
            return state(db, row, local=local, store=self.store)[0] if row else None

    def recover(self):
        """认回会话、恢复已到期的接续，以及中断在退出之后的收尾。"""
        with self.store.local() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM launches WHERE status IN ('running','failed','exited')")]
        adopted = []
        for row in rows:
            attempts = read_attempts(row)
            if row['status'] != 'running':
                if finishing(attempts):
                    attempts[-1]['cleanup'].pop('problem', None)
                    write_attempts(row['log'], attempts)
                    threading.Thread(target=self.cleanup, args=(row['id'],), daemon=True, name=f"launch-cleanup-{row['id']}").start()
                continue
            if row['kind'] == 'run' and attempts and attempts[-1].get('decision_problem'):
                self.pending.add(row['id'])
                threading.Thread(target=self.recheck_exit, args=(row['id'],), daemon=True).start()
                adopted.append(dict(launch=row['id'], pid=None))
                continue
            if row['kind'] == 'run' and attempts and attempts[-1].get('retry_at'):
                self.start_retry(row['id'])
                adopted.append(dict(launch=row['id'], pid=None))
                print(f"已恢复启动 #{row['id']} 的接续等待", flush=True)
                continue
            # 第二次尝试可以比启动记录晚几分钟；进程身份按这次尝试的时间核对。
            current = dict(row, started_at=attempts[-1]['started_at'], pid=attempts[-1].get('pid'),
                           log=attempts[-1]['log']) if attempts else row
            process = adoptable(current)
            if process is None:
                last = attempts[-1] if attempts else {}
                session = last.get('session') or (self.session_from_log(Path(row['log'])) if row['log'] else None)
                if row['kind'] == 'run' and last.get('pid') and not last.get('group_drained'):
                    last['group_drained'] = drain_group(last['pid'])
                    write_attempts(row['log'], attempts)
                error = last.get('error') or ('未发现成功启动的客户端' if not current['pid'] else None)
                self.complete(row['id'], last.get('exit_code'), session, error=error)
                continue
            self.processes[row['id']] = process
            if current['pid'] != row['pid'] or current['log'] != row['log']:
                with self.store.local_tx() as db:
                    db.execute('UPDATE launches SET pid=?, log=?, session=? WHERE id=?',
                               (current['pid'], current['log'], attempts[-1].get('session'), row['id']))
            threading.Thread(target=self.watch, args=(row['id'], process, Path(current['log'])), daemon=True,
                             name=f"launch-{row['id']}").start()
            adopted.append(dict(launch=row['id'], pid=current['pid']))
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


def public(row, runs, *, local, attempts=None, store=None):
    attempts = read_attempts(row) if attempts is None else attempts
    run_views = []
    for run in runs:
        shaped = dict(id=run['id'], kind=run['kind'], status=run['status'])
        if run['kind'] == 'run':
            data = progress.observe(store, run)
            shaped.update(progress.view(run, local=local, data=data),
                          unadjudicated=progress.unadjudicated(run, data))
        run_views.append(shaped)
    item = dict(id=row['id'], kind=row['kind'], agent=row['agent'], scope=json.loads(row['scope']), status=row['status'],
                exit_code=row['exit_code'], requested_by=row['requested_by'], started_at=row['started_at'],
                ended_at=row['ended_at'], checkout=row['checkout'], runs=run_views)
    if row['kind'] == 'run':
        fields = ('attempt', 'session', 'pid', 'log', 'started_at', 'ended_at', 'exit_code', 'retry_at', 'error',
                  'runner', 'model', 'effort', 'new_verdicts', 'decision_problem') if local else (
            'attempt', 'started_at', 'ended_at', 'exit_code', 'retry_at', 'new_verdicts')
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
                    entry['attention_problem'] = (summary['launch_attention_problem'] if local else '请在本机查看原因')
            unsubmitted.append(entry)
        if unsubmitted:
            item['unsubmitted'] = unsubmitted
            details = []
            for run in unsubmitted:
                text = f"{'运行' if run['kind'] == 'run' else '沉淀'} #{run['id']} "
                if run.get('abandoned_by') == 'sop-board':
                    text += '未提交，已由看板放弃（abandoned）'
                    text += (f"，待人处理 #{run['attention']}" if run.get('attention') else
                             f"，待人处理没登记上：{run['attention_problem']}" if run.get('attention_problem') else '，待人处理登记中')
                else:
                    text += '未提交（prepared）' if run['status'] == 'prepared' else '已放弃（abandoned）'
                details.append(text)
            item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + '；' + '、'.join(details)
        elif attempts:
            for attention in attempts[-1].get('cleanup', {}).get('attentions', []):
                item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + (
                    f"；待人处理 #{attention['attention']}" if attention.get('attention') else f"；待人处理没登记上：{attention['problem'] if local else '请在本机查看原因'}")
    stuck = (attempts[-1].get('cleanup', {}).get('problem') or attempts[-1].get('decision_problem')) if attempts else None
    if stuck:
        item['problem'] = item.get('problem', f"启动 #{row['id']} 已退出") + '；' + stuck
    if item['status'] in ('no-work', 'failed') or item.get('unsubmitted'):
        if local:
            item['log_tail'] = log_tail(row['log'])
    if local:
        item.update(pid=row['pid'], log=row['log'], session=row['session'],
                    prompt=str(Path(row['log']).with_suffix('.prompt.md')) if row['log'] else None)
        if row['kind'] == 'run' and row['log']:
            preflight = Path(row['log']).parent / 'preflight.json'
            if preflight.is_file():
                item.update(json.loads(preflight.read_text(encoding='utf-8')))
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
            allow_open_fix=bool(request.json_field('allow_open_fix')), runner=request.json_field('runner'),
            model=request.json_field('model'), effort=request.json_field('effort'), note=request.json_field('note'),
            with_identities=request.json_field('with_identities'))))

    @route('POST', '/local/launch/stop', 'token')
    def stop(app, request):
        from .errors import reject
        launch_id = request.json_field('launch')
        if type(launch_id) is not int:
            reject(422, 'invalid-arguments', 'launch', 'launch stop 须指定 --launch <编号>')
        return json_response(dict(ok=True, launch=app.launcher.stop(launch_id)))


_register()
