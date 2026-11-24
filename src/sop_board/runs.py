"""运行：会话经 CLI 准备与提交场景执行；人工复测标注。

一次运行绑定一个检出、准备时的产品快照与执行者。结果按 检出 + 场景指纹 登记，并记下准备时的快照；
运行中途内容变化的场景只作废这些场景；执行期间产品文件变化的路径记在结果上，只作版本记录。
"""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import hashlib
import json
import math
import os
from pathlib import Path
import re
from string import Template
import shutil
import sys

import yaml

from . import config, daemon, dispositions, gitops, guards, ledger, probes, progress, scenarios, status, works
from .defect_links import normalize as normalize_defect
from .entries import identities as entry_identities, web_evidence
from .errors import APIError, reject
from .store import dumps, loads, now

REASONS = {'new': '未执行', 'guard-failed': '守护未通过', 'failed': '上次不通过', 'rerun': '须实际执行'}
PENDING_STATES = {'not-run': 'new', 'retest': 'guard-failed', 'fail': 'failed', 'blocked': 'failed', 'skip': 'failed'}
RESULT_FIELDS = {'scenario', 'status', 'actual', 'executed_at', 'evidence', 'finding', 'note', 'executor', 'session',
                 'cause', 'suspect_defect', 'reservation', 'gap'}
EVIDENCE_ROOTS = ('evidence', 'steps')   # 自己保存的证据，以及经看板操作留下的页面结构与截图
SHORT_TEXT = 300


def session_id(explicit):
    """执行者：--session，其次启动器注入的 SOP_BOARD_SESSION，再次 Codex 或 Claude Code 自己的会话编号。"""
    value = (explicit or os.environ.get('SOP_BOARD_SESSION') or os.environ.get('CODEX_THREAD_ID')
             or os.environ.get('CLAUDE_CODE_SESSION_ID'))
    if not value or not value.strip():
        reject(422, 'session-required', 'session', '取不到执行者会话编号：请用 --session <会话编号> 指定（Claude Code 用会话的 session_id）')
    return value.strip()


def web_entry(entry):
    return isinstance(entry, dict) and entry.get('surface') == 'web'


def web_identities(entries):
    return {identity for entry in entries if web_entry(entry) for identity in entry_identities(entry)}


def probe_candidates(scope, scenes):
    return [dict(scenario=item['scenario'], fingerprint=scenes[item['scenario']]['fingerprint'],
                 entry=scenes[item['scenario']]['verify']) for item in scope]


def scope_for(context, scenes, derived, requested, fix=None):
    """返回 [{scenario, reason}]。requested：'pending'、'all' 或场景列表。"""
    items = derived['items']
    if fix is not None:
        keys = [scenarios.key(p['file'], p['scenario']) for p in fix['problems']]
        missing = [k for k in keys if k not in scenes]
        if missing:
            reject(409, 'fix-scenario-missing', 'fix', '修复的问题场景已不在当前场景文件中：' + '、'.join(missing))
        return [dict(scenario=k, reason='rerun') for k in keys]
    if requested in (['pending'], ['all']):
        result = []
        held = {item['key'] for item in derived['held']}
        for key in sorted(scenes):
            info = items[key]
            if info['state'] == 'draft' or info['disposition'] or key in held:
                continue
            if requested == ['all']:
                result.append(dict(scenario=key, reason='rerun'))
            elif info['state'] in PENDING_STATES:
                result.append(dict(scenario=key, reason=pending_reason(info)))
        return result
    try:
        keys = scenarios.resolve(requested, scenes)
    except ValueError as error:
        reject(422, 'scenario-not-found', 'scope', str(error))
    drafts = [k for k in keys if items[k]['state'] == 'draft']
    if drafts:
        reject(409, 'draft-not-runnable', 'scope', '草稿场景确认后才能执行：' + '、'.join(drafts))
    return [dict(scenario=key, reason=pending_reason(items[key])) for key in keys]


def pending_reason(info):
    """状态 → 本轮原因；修复关闭导致的待复测须实际执行（rerun）。"""
    if info.get('cause') == 'fix-closed':
        return 'rerun'
    return PENDING_STATES.get(info['state'], 'rerun')


def recorded_item(key, state, result_id, note, cause, suspect_defect, finding, reservation=None, gap=None):
    item = dict(scenario=key, status=state, result=result_id, cause=cause, suspect_defect=bool(suspect_defect))
    for field, value in (('finding', finding), ('note', note), ('reservation', reservation), ('gap', gap)):
        if value:
            item[field] = value
    return item


def write_brief(directory, work_id, scope, entries, browser_problems, fixtures_declared, environment=None,
                invalid_files=(), selected_dispositions=(), selected_holds=(), resolved=(), skipped=(), limit_seconds=progress.LIMIT):
    environment = environment or {}
    counts = {}
    for item in scope:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    lines = [f'# 运行 #{work_id} 速查卡', '', '先读本卡与 prompt.md；参考文档只在遇到对应情况时再读。', '', '## 本轮范围', '',
             f"- {len(scope)} 个场景：" + '，'.join(f"{REASONS[k]} {v}" for k, v in sorted(counts.items())) + '（明细见 pending.json）。']
    for name, entry in sorted(entries.items()):
        extra = ''
        if isinstance(entry, dict) and entry.get('surface') == 'web':
            policy = web_evidence(entry)
            extra = f"，录屏{'开' if policy['video'] else '关'}、trace {'开' if policy['trace'] else '关'}"
        lines.append(f"- 入口 {name}（{entry.get('surface')}）：证据 {'、'.join(entry.get('evidence', []))}{extra}")
    if invalid_files:
        lines += ['', '## 未纳入的不合格文件', '', '在报告中转述以下未纳入范围的文件及问题：', '']
        lines += [f"- {p['file']}：{p['field']}（行 {p.get('line')}）：{p['problem']}" for p in invalid_files]
    if selected_dispositions:
        lines += ['', '## 点名执行的处置场景', '']
        lines += [f"- {item['key']}（{dispositions.LABELS[item['kind']]}）：{item['reason']}"
                  for item in selected_dispositions]
    if selected_holds:
        lines += ['', '## 点名执行的卡住场景', '']
        lines += [f"- {item['key']}（{item['cause']}，待人处理 #{item['attention']}"
                  f"{'，疑似缺陷' if item['suspect_defect'] else ''}）：{item['finding']}" for item in selected_holds]
    if resolved:
        lines += ['', '## 已解决的待人处理', '']
        lines += [f"- {key}（{cause}，#{item['id']}，{item['resolved_by']}，{item['resolved_at']}）：{item['note']}"
                  for key, cause, item in resolved]
    if skipped:
        lines += ['', '## 入口探测排除的场景', '']
        lines += [f"- {item['scenario']}：{item['finding']}。已登记为未执行。" for item in skipped]
    missing = {name: var for name, var in (environment.get('missing_address_env') or {}).items() if name in entries}
    if missing:
        lines += ['', '## 入口地址缺失', '']
        lines += [f'- 入口 {name}：环境没有给出 {var}。用这个入口的场景写 not-executed、cause: environment，finding 写“入口 {name} 的地址 {var} 本机环境没有给出”。'
                  for name, var in sorted(missing.items())]
    services = environment.get('services') or []
    if services:
        lines += ['', '## 服务', '', '准备时各服务的状态（详见 services.json；执行中用 `sop-board env status --json` 看当前状态）：', '']
        for item in services:
            text = f"- {item['name']}（{item['about']}）：{item['label']}"
            if item.get('stoppable'):
                text += '，可停'
            if item.get('log'):
                text += f"，日志 {item['log']}"
            lines.append(text)
        lines += ['',
                  '按场景起点与步骤用到的业务能力，对照括号里的业务说明找到对应的服务：',
                  '',
                  '- 用到的服务本机未搭建、已停止或未上报：写 not-executed、cause: environment，finding 写“<服务>（<业务说明>）本机未搭建”等实际状态。不启动服务、不登记仓库、不改配置。',
                  '- 用到的服务异常退出（准备前或执行中）：写 blocked、cause: environment，finding 附 `env status` 给出的日志末尾。',
                  '- 场景起点要求某个依赖不可用：先 `sop-board env stop <服务>` 停掉对应的可停服务，再执行；结果写好后 `sop-board env start <服务>`，或交给提交时自动恢复。'
                  'executed_at 写服务停着期间的实际执行时刻。对应的服务本机未搭建时写 not-executed、cause: environment；不可停，或依赖是外部共享的中间件时写 blocked、cause: capability 并写明原因。',
                  '- 不用到这些服务的场景照常执行。']
    if browser_problems:
        lines += ['', '## 浏览器问题', '']
        lines += [f'- 身份 {identity}：{problem}。用这个身份的 Web 场景直接写 blocked、cause: environment，finding 照抄这句。'
                  for identity, problem in sorted(browser_problems.items())]
    lines += ['', '## 前置数据', '']
    if fixtures_declared:
        lines += [f'- 无关前置数据用 sop-board fixtures run <配方> --run {work_id} --scenario <场景>；没有合适配方且 adhoc 为 true 时才用 fixtures exec。',
                  '- real_only 对象与被验收流程应产生的对象不得写库代替；准备不了就记 blocked、cause: precondition，finding 写缺什么。']
    else:
        lines.append('- 项目没有声明前置数据配方：前置不满足时记 blocked、cause: precondition，finding 写明缺什么，不自行写库。')
    lines += ['', '## 时间上限与停止', '',
              f'- 本轮停滞提醒上限为 {limit_seconds} 秒：有场景已开始、连续这么久没有新的裁决时，相关命令输出 scenario-timeout 并列出进行中的场景。',
              '- 收到停滞提醒，或同一步试了 4 次仍不成时，记下现象与证据：能判定的先裁决，判断不了的记 blocked，转向下一组。',
              '- 业务断言失败只记该场景失败并继续。',
              '- 等人处理的事（真实账号或未声明账号的登录、人机验证、只发到使用者本人设备的验证码、身份使用次数到上限），不论出现在开始前还是执行中：'
              '该场景写 not-executed、cause: human，finding 写需要人做什么，提交时自动生成待人处理；'
              '不用这件事的场景照常执行。'
              '声明的测试账号未登录时直接用凭据变量登录，不算等人处理。',
              '- 入口看起来整体不可用时先运行 `sop-board env probe <入口>`，再按 prompt.md 判断是否中止该入口。',
              '- 连接异常、状态不明时停止本轮，剩余场景写 not-executed 行（带 cause 与 finding）。']
    (directory / 'brief.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def invalid_files(found):
    return [dict(p, file=str(info['path'])) for info in found['files'].values() for p in info['problems']]


def validate_scope(context, requested, fix=None, *, session=None, launch=None):
    """准备与启动共用的只读前置检查。"""
    from .cli import scene_board
    found, snapshot, derived = scene_board(context)
    scenarios.require_files(found, {value.split('#', 1)[0] for value in requested if '#' in value})
    scope = scope_for(context, found['scenarios'], derived, requested, fix)
    if not scope:
        raise APIError(409, 'nothing-to-run', [dict(field='scope', problem='没有需要执行的场景')] + invalid_files(found),
                       held=excluded_holds(derived, requested))
    with context.store.local() as db:
        scope = without_taken(scope, taken_scenarios(db, context.checkout), requested, fix, session=session, launch=launch)
    if not scope:
        raise APIError(409, 'nothing-to-run', [dict(field='scope', problem='没有需要执行的场景：其余场景都在本检出别的 prepared 运行里')]
                       + invalid_files(found), held=excluded_holds(derived, requested))
    return found, snapshot, derived, scope


def excluded_holds(derived, requested):
    if requested not in (['pending'], ['all']):
        return []
    return [item for item in derived['held'] if not derived['items'][item['key']]['disposition']
            and derived['items'][item['key']]['state'] != 'draft'
            and (requested == ['all'] or derived['items'][item['key']]['state'] in PENDING_STATES)]


def selected_holds(derived, keys):
    return [item for item in derived['held'] if item['key'] in keys]


def taken_scenarios(db, checkout, exclude=None):
    """本检出别的 prepared 运行里的场景 → 运行记录。"""
    taken = {}
    for row in db.execute("SELECT * FROM local_work WHERE checkout=? AND kind='run' AND status='prepared' ORDER BY id", (checkout.id,)):
        if row['id'] != exclude:
            for item in json.loads(row['scope'] or '[]'):
                taken.setdefault(item['scenario'], dict(row))
    return taken


def same_session(work, session, launch):
    return bool((session and work['session'] == session) or (launch and work['launch'] and work['launch'] == launch))


def without_taken(scope, taken, requested, fix=None, *, session=None, launch=None):
    """pending 与 all 跳过别的运行里的场景；明确指定的场景（含修复的问题场景）在别的运行里时返回 scenario-in-run。"""
    if fix is None and requested in (['pending'], ['all']):
        return [item for item in scope if item['scenario'] not in taken]
    clashes = [item['scenario'] for item in scope if item['scenario'] in taken]
    if clashes:
        problems = []
        for key in clashes:
            work = taken[key]
            sessions = json.loads(work['result'] or '{}').get('sessions') or [work['session']]
            own = same_session(work, session, launch)
            owner = '本会话自己准备的运行' if own else '本检出的运行'
            way = (f"继续执行并 run submit，或 run abandon --run {work['id']} 后重新准备" if own
                   else '等它提交或放弃，或换别的场景')
            problems.append(dict(field='scope', problem=f"{key} 已在{owner} #{work['id']} 里（由会话 {sessions[0]} "
                                 f"于 {work['created_at']} 准备）；{way}"))
        raise APIError(409, 'scenario-in-run', problems)
    return scope


def resume_prepared(context, requested, executor, launch, fix_id):
    """只返回有首次准备输出、属于调用会话且覆盖本次范围的运行；不重写运行目录。"""
    with context.store.local() as db:
        candidates = [dict(row) for row in db.execute(
            "SELECT * FROM local_work WHERE checkout=? AND kind='run' AND status='prepared' ORDER BY created_at DESC, id DESC",
            (context.checkout.id,)) if same_session(row, executor, launch) and row['fix'] == fix_id and row['dir']]
    keys = None
    for work in candidates:
        path = Path(work['dir']) / 'prepare.json'
        if not path.is_file():
            continue
        output = json.loads(path.read_text(encoding='utf-8'))
        if fix_id is None and requested not in (['pending'], ['all']):
            if keys is None:
                if all('#' in value for value in requested):
                    keys = set(requested)
                else:
                    found = scenarios.scan(context.checkout.scenarios_dir(context.project), set(context.project.entries))
                    try:
                        keys = set(scenarios.resolve(requested, found['scenarios']))
                    except ValueError:
                        return None  # 由通常的范围检查给出具体错误。
            original = {item['scenario'] for item in output['scope'] + output.get('probe_skipped', [])}
            if not keys <= original:
                continue
        with progress.lock(work):
            work = load_work(context, work['id'])
            if work['status'] != 'prepared':
                continue
            data = progress.recover(context.store, work)
            if work['session'] != executor:
                if not launch or work['launch'] != launch:
                    continue
                result = loads(work['result'], {})
                sessions = result.setdefault('sessions', [work['session']])
                if executor not in sessions:
                    sessions.append(executor)
                with context.store.local_tx() as db:
                    db.execute('UPDATE local_work SET session=?, result=? WHERE id=?', (executor, dumps(result), work['id']))
            return dict(output, session=executor, resumed=True, progress=progress.summary(work, data))
    return None


def prepare(context, requested, *, session=None, fix_id=None, launch=None, identities=None, wait=0, allow_open_fix=False,
            with_identities=None):
    project, checkout, store = context.project, context.checkout, context.store
    launch = launch or (int(os.environ['SOP_BOARD_LAUNCH']) if os.environ.get('SOP_BOARD_LAUNCH', '').isdigit() else None)
    executor = session_id(session)
    resumed = resume_prepared(context, requested, executor, launch, fix_id)
    if resumed is not None:
        return resumed
    operator = config.require_operator()
    fix = None
    if fix_id is not None:
        from . import fixes
        fix = fixes.for_verify(context, fix_id, executor)
    found, snapshot, derived, scope = validate_scope(context, requested, fix, session=executor, launch=launch)
    scenes = found['scenarios']
    checked = daemon.call(project, 'POST', '/local/run/check', dict(
        checkout=checkout.name, session=executor, allow_open_fix=allow_open_fix))
    snapshot = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)
    from .checkouts import branch_of
    # 登记运行与占住场景在同一个本机事务里：并行准备的两个会话不会拿到同一个场景。
    with store.local_tx() as db:
        scope = without_taken(scope, taken_scenarios(db, checkout), requested, fix, session=executor, launch=launch)
        if not scope:
            raise APIError(409, 'nothing-to-run', [dict(field='scope', problem='没有需要执行的场景：其余场景都在本检出别的 prepared 运行里')],
                           held=excluded_holds(derived, requested))
        cursor = db.execute("INSERT INTO local_work(kind,checkout,status,session,operator,implementer,fix,launch,dir,scope,branch,"
                            "created_at) VALUES ('run',?,'prepared',?,?,?,?,?,'',?,?,?)",
                            (checkout.id, executor, operator, fix['session'] if fix else None, fix_id, launch,
                             dumps(scope), branch_of(project, checkout), now()))
        work_id = cursor.lastrowid
    candidates = probe_candidates(scope, scenes)
    entries = {name: project.entries[name] for name in sorted({item['entry'] for item in candidates})}
    directory = None
    holder = f'run:{work_id}'
    try:
        declared = sorted(web_identities(entries.values()))
        if identities:
            unknown = sorted(set(identities) - set(declared))
            if unknown:
                reject(422, 'invalid', 'identity', f"{'、'.join(unknown)} 不是本轮 Web 场景的入口声明的身份；可用："
                       + ('、'.join(declared) or '（本轮没有 Web 场景）'))
        chosen = sorted(set(identities)) if identities else declared
        # 另外取得的身份：场景经一个入口发起、却要用别的入口的身份去核对时点名（例如买家入口的场景要商家后台核对）。
        extra = extra_identities(project, with_identities)
        chosen = sorted(set(chosen) | set(extra))
        entries = {**entries, **{name: project.entries[name] for name in sorted(set(extra.values())) if name not in entries}}
        acquired = daemon.call(project, 'POST', '/local/run/acquire', dict(
            checkout=checkout.name, run=work_id, identities=chosen, extra=sorted(extra), session=executor, pid=os.getpid(), wait=wait,
            candidates=candidates, fixing=fix is not None, digest=snapshot['product_digest'], allow_open_fix=allow_open_fix),
            timeout=max(0, wait) + 1800 + probes.timeout(entries.values()))
        directory = Path(acquired['directory'])
        environment, leased = acquired['environment'], acquired['browsers']
        reports, skipped = acquired['probes'], acquired['probe_skipped']
        excluded = {item['scenario'] for item in skipped}
        scope = [item for item in scope if item['scenario'] not in excluded]
        keys = {item['scenario'] for item in scope}
        # 探测后留下的入口，加上点名另外取得的身份所属的入口（它们不参加探测）。
        entries = {name: entries[name] for name in sorted({scenes[key]['verify'] for key in keys} | set(extra.values()))}
        selected_dispositions = dispositions.listed(derived['items'], keys)
        held = selected_holds(derived, keys)
        resolved = [(key, cause, item) for key in sorted(keys)
                    for cause, item in derived['book'].resolved_holds(key, scenes[key]['fingerprint']).items()]
        browsers = {k: v['endpoint'] for k, v in leased.items() if 'endpoint' in v}
        browser_problems = {k: v['problem'] for k, v in leased.items() if 'problem' in v}
        materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems,
                    fix, invalid_files(found), selected_dispositions=selected_dispositions, held=held, resolved=resolved,
                    skipped=skipped, launch=launch)
        prepared_at = now()
        by_entry = {report['entry']: report['at'] for report in reports}
        data = progress.initial(project.scenario_timeout, prepared_at, [
            probe_adjudication(row, by_entry[scenes[row['scenario']]['verify']]) for row in skipped])
        progress.save(directory, data)
        # 部分就绪：adapter 输出就绪即可执行；各场景再按实际服务状态判断。
        output = dict(ok=True, run=work_id, checkout=checkout.name, directory=str(directory), prompt=str(directory / 'prompt.md'),
                      outputs=dict(results=str(directory / 'results.jsonl')), identities=sorted(leased),
                      switched_from=acquired.get('switched_from'),
                      session=executor, scope=scope, browser_problems=browser_problems, invalid_files=invalid_files(found),
                      dispositions=selected_dispositions, held=held, probes=reports, probe_skipped=skipped,
                      progress=progress.summary(dict(scope=dumps(scope), status='prepared'), data),
                      environment=dict(state=environment['state'], address_env=environment.get('address_env', {})),
                      services=public_services(environment.get('services', [])), not_running=environment.get('not_running', []),
                      missing_address_env=environment.get('missing_address_env', {}))
        (directory / 'prepare.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        product = {k: v for k, v in snapshot.items() if k != 'files'}
        result = dict(sessions=[executor])
        allowed = acquired.get('allowed_open_fix') or checked.get('allowed_open_fix')
        if allowed is not None:
            result['allowed_open_fix'] = allowed
        with store.lock:
            if skipped:
                with store.local() as db:
                    work = dict(db.execute('SELECT * FROM local_work WHERE id=?', (work_id,)).fetchone())
                work.update(scope=dumps(scope), snapshot=dumps(product), product_digest=snapshot['product_digest'])
                # 共享库提交前先核对本机更新可写。单机库用同一个事务更新运行与业务记录。
                with (store.local_tx() if store.shared else nullcontext()) as local, store.records_tx() as db:
                    record, result = publish_probe_skips(db, context, work, result, skipped, scenes, reports)
                    finish_prepare(db if local is None else local, work_id, directory, scope, product, result, record)
            else:
                with store.local_tx() as db:
                    finish_prepare(db, work_id, directory, scope, product, result, None)
    except BaseException:
        with store.local_tx() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (holder,))
            db.execute('DELETE FROM local_work WHERE id=?', (work_id,))
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return output


def finish_prepare(db, work_id, directory, scope, snapshot, result, record):
    db.execute('UPDATE local_work SET dir=?, scope=?, snapshot=?, product_digest=?, result=?, record=? WHERE id=?',
               (str(directory), dumps(scope), dumps(snapshot), snapshot['product_digest'], dumps(result), record, work_id))


def probe_adjudication(row, at):
    return progress.item(row['scenario'], row['fingerprint'], 'not-executed', at, registration='registered',
                         cause=row['cause'], finding=row['finding'], source='probe')


def publish_probe_skips(db, context, work, result, skipped, scenes, reports):
    from .redaction import business_text
    store, checkout = context.store, context.checkout
    times = {report['entry']: report['at'] for report in reports}
    operator = work['operator']
    db.lock_sequence()
    record, fresh = works.publish(db, store, work, checkout=checkout.name, branch=work['branch'], prepared=True)
    if not fresh:
        return record, loads(db.value('SELECT result FROM work WHERE id=?', (record,)), {})
    items = []
    for row in skipped:
        item = dict(row, by=operator, at=times[scenes[row['scenario']]['verify']], source='probe',
                    seq=db.next_seq(), suspect_defect=False,
                    finding=business_text(context.project, checkout, row['finding']))
        ledger.register_hold(db, store, checkout, work['id'], item, item['seq'], by=operator)
        items.append(item)
    result = dict(result, not_executed=items, adjudications={
        item['scenario']: dict(probe_adjudication(item, item['at']), seq=item['seq'], by=operator,
                               independence=None, row_sha256=None) for item in items})
    db.execute('UPDATE work SET result=? WHERE id=?', (dumps(result), record))
    return record, result


def public_services(services):
    return [{k: v for k, v in item.items() if k in ('name', 'about', 'stoppable', 'state', 'label', 'log')} for item in services]


def extra_identities(project, names):
    """--with-identity 点名的身份 → 声明它的 Web 入口；须是项目里某个 Web 入口声明的身份。"""
    owners = {identity: name for name, entry in project.entries.items() if web_entry(entry) for identity in entry_identities(entry)}
    unknown = sorted(set(names or ()) - set(owners))
    if unknown:
        reject(422, 'invalid', 'with-identity', f"{'、'.join(unknown)} 不是项目里 Web 入口声明的身份；可用：" + '、'.join(sorted(owners)))
    return {identity: owners[identity] for identity in names or ()}


def launch_note(context, launch):
    """run launch --note 写下的本轮说明；没有时为 None。"""
    if launch is None:
        return None
    from .launcher import NOTE_FILE
    with context.store.local() as db:
        row = db.execute('SELECT log FROM launches WHERE id=?', (launch,)).fetchone()
    if row is None or not row['log']:
        return None
    path = Path(row['log']).parent / NOTE_FILE
    return path.read_text(encoding='utf-8').strip() if path.is_file() else None


def materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems, fix,
                invalid_files=(), *, selected_dispositions=(), held=(), resolved=(), skipped=(), launch=None):
    project, checkout = context.project, context.checkout
    reasons = {item['scenario']: item['reason'] for item in scope}
    selected = [dict(scenes[key], key=key, reason=reasons[key]) for key in reasons]
    (directory / 'evidence').mkdir()
    (directory / 'scenarios.yaml').write_text(yaml.safe_dump(
        [{k: v for k, v in scene.items() if k not in ('status',)} for scene in selected],
        allow_unicode=True, sort_keys=False), encoding='utf-8')
    (directory / 'pending.json').write_text(dumps(scope), encoding='utf-8')
    addresses = environment.get('address_env', {})
    (directory / 'entries.json').write_text(dumps(dict(entries=entries, addresses={
        name: addresses.get(entry.get('address_env')) for name, entry in entries.items() if entry.get('address_env')})), encoding='utf-8')
    (directory / 'repos.json').write_text(dumps({alias: str(path) for alias, path in checkout.paths.items()}), encoding='utf-8')
    (directory / 'snapshot.json').write_text(dumps({k: v for k, v in snapshot.items() if k != 'files'}), encoding='utf-8')
    (directory / 'product-files.json').write_text(dumps(snapshot['files']), encoding='utf-8')
    # 准备时各服务的状态与业务说明，供执行会话把场景起点对应到服务；提交时据此区分运行期间才异常退出的服务。
    (directory / 'services.json').write_text(json.dumps(dict(
        at=now(), environment=environment.get('state'), services=environment.get('services', []),
        not_running=environment.get('not_running', []), missing_address_env=environment.get('missing_address_env', {})),
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    web = [scene for scene in selected if web_entry(entries[scene['verify']])]
    env_lines = {**addresses}
    runtime = None
    leased = set(browsers) | set(browser_problems)
    # 本轮没有 Web 场景、但点名另外取得了身份时（--with-identity），同样写出身份材料。
    if web or leased:
        from . import browsers as broker
        from .playwright import write_scope
        runtime = broker.runtime_info()
        write_scope(directory / 'scope.json', project.name, checkout.name, web, entries, directory / 'scenarios.yaml')
        identities = {name: [dict(item, endpoint=browsers.get(item['id'])) for item in entry.get('identities', []) if item['id'] in leased]
                      or ([dict(id='default', label='default', endpoint=browsers.get('default'))] if not entry.get('identities') else [])
                      for name, entry in entries.items() if web_entry(entry)}
        (directory / 'identities.json').write_text(dumps(dict(entries=identities)), encoding='utf-8')
        maps = atlas_excerpt(context, entries)
        if maps:
            (directory / 'atlas.json').write_text(dumps(dict(entries=maps)), encoding='utf-8')
            env_lines['PW_ATLAS'] = str(directory / 'atlas.json')
            if len(maps) == 1:
                env_lines['PW_ATLAS_ENTRY'] = maps[0]['entry']
        endpoint = browsers.get('default') or next(iter(browsers.values()), None)
        if endpoint:
            env_lines['PW_CDP_ENDPOINT'] = endpoint
        env_lines.update(PW_IDENTITIES=str(directory / 'identities.json'), PW_SCOPE=str(directory / 'scope.json'),
                         PW_SCENARIO_FILE=str(directory / 'scenarios.yaml'), PW_OUT=str(directory / 'playwright'))
        if runtime:
            env_lines.update(PLAYWRIGHT_BROWSERS_PATH=runtime['browsers'], PW_NODE_ROOT=str(Path(runtime['node_modules']).parent))
            (directory / 'node_modules').symlink_to(runtime['node_modules'], target_is_directory=True)
    env_lines.update(SOP_BOARD_RUN=str(work_id), SOP_BOARD_CHECKOUT=checkout.name)
    (directory / 'run.env').write_text(''.join(f"{name}={value}\n" for name, value in sorted(env_lines.items())), encoding='utf-8')
    write_brief(directory, work_id, scope, entries, browser_problems, project.fixtures is not None, environment, invalid_files,
                selected_dispositions, held, resolved, skipped, project.scenario_timeout)
    if (directory / 'atlas.json').exists():
        with (directory / 'brief.md').open('a', encoding='utf-8') as brief:
            brief.write('\n## 交互地图\n\n- atlas.json 是仓库中的交互地图（PW_ATLAS）：用模板 atlas.mjs 按页面与控件操作；'
                        'stale 列出的页面已过期，以实际页面为准。地图不提供预期。找不到时只读探测，并在结果 note 写明地图缺口。\n')
    from . import definitions, notes
    try:
        # 本机的产品级定义随准备发布到共享副本，供新成员加入时预填；失败不影响这次准备。
        definitions.publish(context.store, project, by=config.operator() or "sop-board")
    except Exception as error:  # noqa: BLE001 - 发布只是同步副本
        print(f'产品定义发布失败（不影响本轮）：{error}', file=sys.stderr)
    with (directory / 'brief.md').open('a', encoding='utf-8') as brief:
        round_note = launch_note(context, launch)
        if round_note:
            brief.write(f'\n## 本轮说明（启动 #{launch}）\n\n发起这次启动的人写给本轮的安排，只对本轮有效；与下面的产品备注冲突时以本轮说明为准。\n\n{round_note}\n')
        related = notes.relevant(context.store, entries, {scene['file'] for scene in selected})
        brief.write('\n'.join(notes.brief_lines(related, work_id)) + '\n')
        with context.store.records() as db:
            gaps = [row['gap'] for row in db.all(
                'SELECT gap, MAX(seq) AS latest FROM results WHERE product=? AND gap IS NOT NULL GROUP BY gap ORDER BY latest DESC LIMIT 20',
                (context.store.product,))]
        if gaps:
            brief.write('\n## 已登记过的前置缺口\n\n本轮遇到同一个缺口时，结果行的 gap 照抄这里的写法：\n\n'
                        + ''.join(f'- {gap}\n' for gap in gaps))
    prompt = Template(files('sop_board').joinpath('prompts', 'run.md').read_text(encoding='utf-8')).substitute(
        run=work_id, directory=directory,
        checkout=checkout.name, verify_note=('这是修复 #%d 的独立复验：逐条完整执行，不能保留旧通过，也不接受 not-executed；'
                                 '本机缺少条件时停下报告缺什么。' % fix['id']) if fix else '')
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    from .redaction import clean_task_output
    clean_task_output(directory, ())


def atlas_excerpt(context, entries):
    """本轮 Web 入口在仓库中的交互地图（含过期页面），供模板 atlas.mjs 按页面与控件操作。"""
    from . import atlas
    result = []
    for name, entry in entries.items():
        if not (web_entry(entry) and isinstance(entry.get('atlas'), dict) and entry.get('spec_library')):
            continue
        target, _ = atlas.paths(context.project, context.checkout, name)
        if not target.is_file():
            continue
        document = json.loads(target.read_text(encoding='utf-8'))
        stale = atlas.stale(context, name) or []
        result.append(dict(entry=name, pages=document['pages'],
                           index=[dict(id=page['id'], title=page['title']) for page in document['pages']],
                           stale=stale))
    return result


# ---- 提交 ----

def load_work(context, run_id, kind='run'):
    """本机运行（本机状态库）；运行属于另一个检出时切到那个检出。"""
    with context.store.local() as db:
        row = db.execute('SELECT * FROM local_work WHERE id=? AND kind=?', (run_id, kind)).fetchone()
        if row is None:
            reject(404, 'run-not-found', 'run', f'运行不存在：#{run_id}')
        if row['checkout'] != context.checkout.id:
            from . import checkouts
            context.checkout = checkouts.by_id(db, row['checkout'])
    return dict(row)


def _strict_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('重复字段：' + name)
            result[name] = value
        return result
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('非有限数字')
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_float=finite, parse_constant=finite)


def invalid_result(field, problem, *, key=None, line=None):
    return APIError(422, 'result-invalid', [dict(file='results.jsonl', scenario=key, line=line, field=field, problem=problem)])


def resolve_key(scope, value):
    if isinstance(value, str):
        if value in scope:
            return value
        matches = [key for key in scope if key.partition('#')[2] == value]
        if len(matches) == 1:
            return matches[0]
    return None


def read_results(directory, scope, *, target=None):
    path = directory / 'results.jsonl'
    if path.is_symlink() or path.exists() and not path.is_file():
        raise invalid_result('results.jsonl', '结果文件须为普通文件')
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        contents = b''
    rows, lines, malformed = {}, {}, []
    partial = None
    raw_lines = contents.split(b'\n')
    for number, raw in enumerate(raw_lines, 1):
        try:
            line = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            if target is None and number == len(raw_lines) and error.reason == 'unexpected end of data':
                partial = number
                continue
            problem = invalid_result('results.jsonl', '结果行须为 UTF-8', line=number)
            if target is None:
                raise problem from error
            malformed.extend(problem.problems)
            continue
        if not line.strip():
            continue
        try:
            row = _strict_json(line)
        except ValueError as error:
            if target is None and number == len(raw_lines) and isinstance(error, json.JSONDecodeError):
                partial = number
                continue
            problem = invalid_result('results.jsonl', f'不是合格的 JSON：{error}', line=number)
            if target is None:
                raise problem from error
            match = re.search(r'"scenario"\s*:\s*("(?:[^"\\]|\\.)*")', line)
            try:
                key = resolve_key(scope, json.loads(match[1])) if match else None
            except ValueError:
                key = None
            if key == target:
                problem.problems[0]['scenario'] = target
                raise problem from error
            if key is None:
                malformed.extend(problem.problems)
            continue
        value = row.get('scenario') if isinstance(row, dict) else None
        key = resolve_key(scope, value)
        if target is not None and key != target:
            if key is None and value is None:
                malformed.extend(invalid_result('scenario', '结果行没有可读取的场景键', line=number).problems)
            continue
        if key is None:
            raise invalid_result('scenario', f'结果不在本轮范围内：{value}', line=number, key=value)
        if key in rows:
            raise invalid_result('scenario', f'结果重复：{key}', line=number, key=key)
        rows[key], lines[key] = row, number
    if target is not None and target not in rows:
        error = invalid_result('scenario', f'{target} 缺少完整结果行', key=target)
        error.problems.extend(malformed)
        raise error
    return rows, lines, partial


def validate_row(directory, key, row, *, line=None, fixing=False):
    def invalid(field, problem):
        raise invalid_result(field, f'{key}：{problem}', key=key, line=line)

    extra = sorted(set(row) - RESULT_FIELDS)
    if extra:
        invalid(extra[0], '结果行有表外字段：' + '、'.join(extra) + '。每条场景都须实际执行，不能保留旧通过')
    state = row.get('status')
    if state not in ('pass', 'fail', 'blocked', 'skip', 'not-executed'):
        invalid('status', 'status 应为 pass、fail、blocked、skip 或 not-executed')
    if state in ('blocked', 'not-executed'):
        cause = row.get('cause')
        if not isinstance(cause, str) or cause not in ledger.CAUSE_KINDS:
            invalid('cause', 'cause 须为 ' + '、'.join(ledger.CAUSE_KINDS))
    elif 'cause' in row:
        invalid('cause', 'cause 只用于 blocked 或 not-executed')
    if 'suspect_defect' in row and (state != 'blocked' or row['suspect_defect'] is not True):
        invalid('suspect_defect', 'suspect_defect 只用于 blocked，值须为 JSON true')
    if state == 'not-executed' and fixing:
        invalid('status', '独立复验须完整执行，不接受未执行标记')
    for field, allowed, where in (('reservation', state == 'pass', 'pass'),
                                  ('gap', state == 'blocked' and row.get('cause') == 'precondition', 'blocked、cause: precondition')):
        if field in row:
            value = row[field]
            if not allowed or not isinstance(value, str) or not value.strip() or len(value) > SHORT_TEXT:
                invalid(field, f'{field} 只用于 {where}，须为不超过 {SHORT_TEXT} 字的非空文字')
    if (state != 'not-executed' or 'actual' in row) and not (isinstance(row.get('actual'), str) and row['actual'].strip()):
        invalid('actual', 'actual 须为非空文字')
    if state != 'not-executed' or 'executed_at' in row:
        try:
            stamp = datetime.fromisoformat(str(row.get('executed_at', '')).replace('Z', '+00:00'))
            if stamp.utcoffset() is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError()
        except ValueError:
            invalid('executed_at', 'executed_at 须为带时区的实际执行时间')
    if state in ('blocked', 'skip', 'not-executed') and not (isinstance(row.get('finding'), str) and row['finding'].strip()):
        invalid('finding', f'{state} 须写非空 finding')
    if row.get('finding') is not None and not isinstance(row['finding'], str):
        invalid('finding', 'finding 须为文字或 null')
    if row.get('note') is not None and not isinstance(row['note'], str):
        invalid('note', 'note 须为文字或 null')
    evidence = row.get('evidence', [])
    if not isinstance(evidence, list):
        invalid('evidence', 'evidence 应为路径数组')
    if state == 'not-executed':
        if evidence:
            invalid('evidence', '未执行不附证据')
        return
    for name in evidence:
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or Path(name).parts[:1] not in [(root,) for root in EVIDENCE_ROOTS] or len(Path(name).parts) < 2):
            invalid('evidence', '证据须为 evidence/ 或 steps/ 下的相对路径')
        path = directory / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to((directory / Path(name).parts[0]).resolve()):
            invalid('evidence', f'证据文件不存在或越界：{name}')
        if path.stat().st_size > 64 * 1024 * 1024:
            invalid('evidence', f'证据超过 64 MiB：{name}')
    if state in ('pass', 'fail') and not evidence:
        invalid('evidence', '实际执行的 pass、fail 至少要有一份真实证据')


INTERRUPTIONS = 'interruptions.json'


def note_interruption(directory, *, start=None, end=None, error=None):
    """在运行目录记下“环境随看板重启中断”：旧守护进程交接时写开始，新守护进程重新拉起环境后写结束（已定决策 28）。"""
    path = Path(directory) / INTERRUPTIONS
    items = interruptions(directory)
    if start:
        items.append({'reason': 'board-restart', 'text': '环境随看板重启中断', 'from': start, 'to': None})
    if end:
        for item in items:
            if item.get('to') is None:
                item['to'] = end
                if error:
                    item['error'] = error
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def interruptions(directory):
    try:
        items = json.loads((Path(directory) / INTERRUPTIONS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


def active_session(work, explicit=None):
    # 与准备命令同一取法：--session 优先。准备时显式给了会话编号的执行者，裁决时也用它。
    supplied = (explicit or os.environ.get('SOP_BOARD_SESSION') or os.environ.get('CODEX_THREAD_ID')
                or os.environ.get('CLAUDE_CODE_SESSION_ID'))
    if supplied and supplied.strip() != work['session']:
        reject(409, 'run-session-mismatch', 'session', f"运行 #{work['id']} 当前由会话 {work['session']} 执行，接续前先重新准备")
    return work['session']


@contextmanager
def locked_run(context, run_id, *, allow_ended=False):
    if run_id is None:
        reject(422, 'run-required', 'run', '请指定 --run 或载入运行目录的 run.env')
    work = load_work(context, run_id)
    if not work['dir']:
        reject(409, 'run-not-prepared', 'run', f'运行 #{run_id} 尚未准备完成')
    with progress.lock(work):
        work = load_work(context, run_id)
        if not allow_ended and work['status'] != 'prepared':
            reject(409, 'run-not-prepared', 'run', f"运行 #{run_id} 的状态是 {work['status']}")
        try:
            yield work, Path(work['dir'])
        except APIError as error:
            if work['status'] == 'prepared':
                data = progress.read(work)
                error.details.setdefault('warnings', progress.warning(data, now()))
            raise


def run_key(work, value):
    key = resolve_key(progress.scope(work), value)
    if key is None:
        reject(422, 'scenario-not-in-run', 'scenario', f'场景 {value} 不在本轮可执行范围内或编号不唯一')
    return key


def concluded(key):
    reject(409, 'scenario-concluded', 'scenario', f'{key} 本轮已有结论，请用之后的运行或人工标注登记新结论')


def is_concluded(entry):
    return entry and entry['registration'] == 'registered' and entry['status'] in ('pass', 'fail')


def begin(context, run_id, scenarios, *, session=None):
    """一次开始一组场景（已定决策 31）。全部键先核对，有一条不合格就整条拒绝，不开始任何一条。"""
    with locked_run(context, run_id) as (work, directory):
        executor = active_session(work, session)
        keys = list(dict.fromkeys(run_key(work, value) for value in scenarios))
        data = progress.recover(context.store, work)
        if data.get('ended_at'):
            reject(409, 'run-not-prepared', 'run', f'运行 #{run_id} 已完成业务提交')
        for key in keys:
            if is_concluded(data['adjudications'].get(key)):
                concluded(key)
        active, at, begun = dict(progress.running(data)), now(), []
        for key in keys:
            duplicate = bool(active.get(key) and active[key]['session'] == executor)
            if not duplicate:
                active[key] = dict(scenario=key, started_at=at, session=executor)
            begun.append(dict(scenario=key, started_at=active[key]['started_at'], duplicate=duplicate))
        if not all(item['duplicate'] for item in begun):
            data.update(active=active, last_activity_at=at)
            progress.save(directory, data)
        return dict(ok=True, run=run_id, begun=begun, progress=progress.summary(work, data),
                    warnings=progress.warning(data, at))


def declaration(work, data, implementer, implemented_by):
    locked = data.get('implementation')
    explicit = implementer is not None or implemented_by is not None
    if locked is not None and not explicit:
        return locked
    if work['fix'] is not None:
        selected = dict(implementer=work['implementer'], implemented_by=None)
    else:
        if not explicit:
            implementer = os.environ.get('SOP_BOARD_IMPLEMENTER')
            implemented_by = None if implementer else os.environ.get('SOP_BOARD_IMPLEMENTED_BY')
        if implementer and implemented_by:
            reject(422, 'invalid-arguments', 'implementer', '实现者会话和操作者参数互斥')
        implemented_by = implemented_by.strip() if implemented_by is not None else None
        if implemented_by is not None and (not implemented_by or implemented_by == (config.operator() or work['operator'])):
            reject(422, 'invalid-arguments', 'implemented-by', '该参数须为另一位操作者姓名')
        selected = dict(implementer=implementer or None, implemented_by=implemented_by)
    if locked is not None and explicit:
        given = dict(implementer=implementer, implemented_by=implemented_by.strip() if implemented_by is not None else None)
        if given != locked:
            reject(409, 'run-implementer-conflict', 'implementer', '本轮首次裁决已锁定实现者声明')
    return selected


def source_scenes(context, directory):
    prepared = {row['key']: row for row in yaml.safe_load((directory / 'scenarios.yaml').read_text(encoding='utf-8'))}
    current = scenarios.scan(context.checkout.scenarios_dir(context.project), set(context.project.entries))['scenarios']
    return prepared, current


def changed_paths(context, directory):
    before = loads((directory / 'product-files.json').read_text(encoding='utf-8'))
    project = context.project
    after = gitops.snapshot(context.checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)['files']
    return gitops.changed_between(before, after)


def row_hash(row):
    raw = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def evidence_of(directory, row, key, *, expected=None, save_to=None):
    result = []
    for name in row.get('evidence', []):
        try:
            raw = (directory / name).read_bytes()
        except OSError as error:
            raise invalid_result('evidence', f'证据无法读取：{name}', key=key) from error
        digest = hashlib.sha256(raw).hexdigest()
        if expected is not None and expected.get(name) != digest:
            raise invalid_result('evidence', f'证据自裁决后发生变化：{name}', key=key)
        if save_to is not None:
            save_to.put_evidence(raw, Path(name).name)
        result.append((name, digest, len(raw)))
    return result


def create_adjudication(directory, scene, row, line, state, executor, implementation, changed, *, source, crash=None, save_evidence_to=None):
    key = scene['key']
    original = row_hash(row)
    active = progress.running(state).get(key)
    started = active['started_at'] if active and active['session'] == executor else None
    validate_row(directory, key, row, line=line, fixing=False)
    if crash and row['status'] != 'pass':
        row = dict(row, status='blocked', cause='environment', actual=crash, finding=crash, executed_at=now())
        row.pop('suspect_defect', None)
    row = dict(row)
    if row.get('finding'):
        row['finding'] = row['finding'].strip()
    try:
        evidence = evidence_of(directory, row, key, save_to=save_evidence_to if source == 'submit'
                               or row['status'] in ('pass', 'fail') else None)
    except APIError as error:
        error.problems[0]['line'] = line
        raise
    at = now()
    duration = progress.elapsed(started, at)
    metadata = dict(source=source, started_at=started, duration_ms=duration,
                    timed_out=started is not None and bool(progress.warning(state, at)), row_sha256=original)
    independence = 'independent' if implementation['implemented_by'] else status.independence(executor, implementation['implementer'])
    entry = progress.item(key, scene['fingerprint'], row['status'], at, session=executor, cause=row.get('cause'),
                          suspect_defect=bool(row.get('suspect_defect')), finding=row.get('finding'),
                          row=row, evidence_sha256={path: sha for path, sha, _ in evidence},
                          product_changed=changed, independence=independence, **metadata)
    for field in ('note', 'reservation', 'gap'):
        if row.get(field):
            entry[field] = row[field].strip() if field != 'note' else row[field]
    return entry, evidence


def void_entry(scene, *, source, executor, data):
    at = now()
    active = progress.running(data).get(scene['key'])
    started = active['started_at'] if active else None
    return progress.item(scene['key'], scene['fingerprint'], 'voided', at, session=executor, registration='voided',
                         source=source, started_at=started, duration_ms=progress.elapsed(started, at), timed_out=False)


def set_adjudication(data, entry):
    key = entry['scenario']
    data['adjudications'][key] = entry
    data['active'] = {other: item for other, item in progress.running(data).items() if other != key}
    data['last_activity_at'] = entry['at']
    if entry['source'] == 'verdict' and entry['status'] != 'voided':
        keys = data.setdefault('sessions', {}).setdefault(entry['session'], [])
        if key not in keys:
            keys.append(key)


def register_adjudication(db, context, work, record, scene, entry, evidence, implementation, operator):
    """登记这一条并返回可进入共享运行摘要的对象。"""
    from .redaction import names, response_bytes
    project, store, checkout = context.project, context.store, context.checkout
    row = entry['row']
    seq = db.next_seq()
    finding = entry['finding']
    if finding:
        finding = response_bytes(finding.encode(), 'text/plain', names(project.entries), local=True).decode()
    result_id = None
    if entry['status'] != 'not-executed':
        result_id = db.insert(
            'INSERT INTO results(product,seq,work,machine,checkout,branch,file,scenario,fingerprint,product_digest,'
            'product_changed,kind,status,actual,finding,note,executor,implementer,implemented_by,independence,executed_at,'
            'cause,suspect_defect,reservation,gap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (store.product, seq, record, store.machine['id'], checkout.name, work['branch'], scene['file'], scene['id'],
             scene['fingerprint'], work['product_digest'], dumps(entry['product_changed']), 'agent', entry['status'], row['actual'],
             finding, row.get('note'), entry['session'], implementation['implementer'], implementation['implemented_by'],
             entry['independence'], row['executed_at'], entry['cause'], int(entry['suspect_defect']),
             entry.get('reservation'), entry.get('gap')))
        for ordinal, (path, sha, size) in enumerate(evidence):
            name = Path(path).name
            store.index_evidence(db, sha, size, name)
            db.execute('INSERT INTO result_evidence(result, ordinal, name, sha256) VALUES (?,?,?,?)', (result_id, ordinal, name, sha))
        guards.after_result(db, store, checkout.name, scene, entry['status'], result_id)
    entry.update(registration='registered', result=result_id, finding=finding, seq=seq)
    published = dict(progress.public_item(entry), seq=seq, source=entry['source'], row_sha256=entry['row_sha256'],
                     independence=entry['independence'])
    if entry['status'] == 'not-executed':
        published.update(by=operator, at=row.get('executed_at', entry['at']))
    if entry['cause']:
        ledger.register_hold(db, store, checkout, work['id'], published, seq, by=operator)
    return published


def registered_summary(summary, adjudications):
    summary['adjudications'] = adjudications
    records = sorted(adjudications.values(), key=lambda item: item['seq'])
    summary['recorded'] = [recorded_item(r['scenario'], r['status'], r['result'], r.get('note'), r['cause'],
                                         r['suspect_defect'], r['finding'], r.get('reservation'), r.get('gap'))
                           for r in records if r['result'] is not None]
    # 由探测在 A 中登记过、但还没有 adjudications 的运行也保留原来的未执行。
    summary['not_executed'] = [row for row in summary.get('not_executed', []) if row['scenario'] not in adjudications]
    summary['not_executed'] += [row for row in records if row['status'] == 'not-executed']


def independence_summary(data):
    values = {r['independence'] for r in data['adjudications'].values()
              if 'independence' in r and r['registration'] == 'registered'}
    return next(iter(values)) if len(values) == 1 else 'mixed' if values else None


def work_events(context, work, data):
    with context.store.local() as db:
        events = [dict(r) for r in db.execute('SELECT * FROM service_events WHERE work=? ORDER BY id', (work['id'],))]
    rows = {key: r['row'] for key, r in data['adjudications'].items()
            if r['registration'] == 'registered' and r.get('row', {}).get('executed_at')
            and r['status'] != 'not-executed'}
    for event in events:
        if event['action'] == 'stop':
            event['scenarios'] = dumps(sorted(set(loads(event['scenarios'], []))))
    by_event = scenarios_while_stopped(events, rows)
    for event in events:
        stopped = by_event.get(event['id'], [])
        event['scenarios'] = dumps(sorted(set(loads(event['scenarios'], [])) | set(stopped)))
    return events


def business_run(db, context, work, *, existing=None):
    if existing:
        return existing['id'], loads(existing['result'], {})
    record, _ = works.publish(db, context.store, work, checkout=context.checkout.name, branch=work['branch'], prepared=True)
    return record, {}


def update_business(db, record, summary, events, work, implementation, independence, *, ended_at=None):
    db.execute('UPDATE work SET result=?, services=?, session=?, implementer=?, implemented_by=?, independence=?, '
               'ended_at=? WHERE id=?',
               (dumps(summary), dumps([service_event(e) for e in events]), work['session'], implementation['implementer'],
                implementation['implemented_by'], None if independence == 'mixed' else independence, ended_at, record))


def verdict(context, run_id, scenario, *, implementer=None, implemented_by=None, session=None):
    with locked_run(context, run_id) as (work, directory):
        executor, key = active_session(work, session), run_key(work, scenario)
        store = context.store
        data = progress.recover(store, work)
        if data.get('ended_at'):
            reject(409, 'run-not-prepared', 'run', f'运行 #{run_id} 已完成业务提交')
        previous = data['adjudications'].get(key)
        at = now()
        active, warnings = progress.running(data).get(key), progress.warning(data, at)
        if is_concluded(previous):
            try:
                rows, _, _ = read_results(directory, progress.scope(work), target=key)
            except APIError:
                concluded(key)
            if row_hash(rows[key]) != previous.get('row_sha256'):
                concluded(key)
            declaration(work, data, implementer, implemented_by)
            entry, duplicate = previous, True
        else:
            implementation = declaration(work, data, implementer, implemented_by)
            prepared, current = source_scenes(context, directory)
            scene = prepared[key]
            if current.get(key, {}).get('fingerprint') != scene['fingerprint'] or previous and previous['status'] == 'voided':
                entry = previous if previous and previous['status'] == 'voided' else void_entry(
                    scene, source='verdict', executor=executor, data=data)
                duplicate = entry is previous
                if not duplicate:
                    set_adjudication(data, entry)
                    data['implementation'] = implementation
                    progress.save(directory, data)
                elif active:
                    data['active'] = {other: item for other, item in progress.running(data).items() if other != key}
                    data['last_activity_at'] = at
                    progress.save(directory, data)
            else:
                rows, lines, _ = read_results(directory, progress.scope(work), target=key)
                row = rows[key]
                if work['fix'] is not None and row.get('status') == 'not-executed':
                    validate_row(directory, key, row, line=lines[key], fixing=True)
                crash = crash_findings(context, work, {key: scene}).get(key)
                entry, evidence = create_adjudication(directory, scene, row, lines[key], data, executor, implementation,
                                                       changed_paths(context, directory), source='verdict', crash=crash, save_evidence_to=store)
                duplicate = bool(previous and previous['registration'] == 'local' and
                                 all(entry[field] == previous[field] for field in
                                     ('row_sha256', 'started_at', 'session', 'status', 'finding', 'evidence_sha256')))
                # 重试没有开始时刻的暂记，先前的裁决可能已经清除了计时。
                if previous and not active and previous['registration'] == 'local':
                    duplicate = all(entry[field] == previous[field] for field in
                                    ('row_sha256', 'session', 'status', 'finding', 'evidence_sha256'))
                if duplicate:
                    entry = previous
                elif entry['status'] in ('pass', 'fail'):
                    with store.records_tx() as db:
                        db.lock_sequence()
                        existing = progress.business(db, store, run_id)
                        record, summary = business_run(db, context, work, existing=existing)
                        adjudications = summary.get('adjudications', {})
                        published = register_adjudication(db, context, work, record, scene, entry, evidence, implementation,
                                                          config.operator() or work['operator'])
                        adjudications[key] = published
                        registered_summary(summary, adjudications)
                        set_adjudication(data, entry)
                        data['implementation'] = implementation
                        independence = independence_summary(data)
                        update_business(db, record, summary, work_events(context, work, data), work, implementation, independence)
                        progress.stage(directory, data, {key: published['seq']})
                    progress.publish(directory, data)
                    with store.local_tx() as db:
                        db.execute('UPDATE local_work SET record=? WHERE id=?', (record, run_id))
                else:
                    data['implementation'] = implementation
                    set_adjudication(data, entry)
                    progress.save(directory, data)
        return dict(ok=True, run=run_id, scenario=key, adjudication=progress.public_item(entry), duplicate=duplicate,
                    progress=progress.summary(work, data), warnings=warnings)


def submit(context, run_id, *, implementer=None, implemented_by=None):
    store = context.store
    with locked_run(context, run_id) as (work, directory):
        data = progress.recover(store, work)
        operator = config.operator() or work['operator']
        with store.records_tx() as db:
            db.lock_sequence()
            existing = progress.business(db, store, run_id)
            if existing is not None and existing['ended_at'] is not None:
                declaration(work, data, implementer, implemented_by)
                record, summary = existing['id'], loads(existing['result'], {})
                summary['resubmitted'] = True
                ended = existing['ended_at']
                events = work_events(context, work, data)
            else:
                implementation = declaration(work, data, implementer, implemented_by)
                prepared, current = source_scenes(context, directory)
                scope = progress.scope(work)
                rows, lines, partial = read_results(directory, scope)
                changed = set(changed_paths(context, directory))
                crashes = crash_findings(context, work, prepared)
                candidates = {}
                for key in sorted(scope):
                    entry = data['adjudications'].get(key)
                    if is_concluded(entry):
                        continue
                    scene = prepared[key]
                    if entry and entry['status'] == 'voided':
                        continue
                    if current.get(key, {}).get('fingerprint') != scene['fingerprint']:
                        set_adjudication(data, void_entry(scene, source='submit', executor=work['session'], data=data))
                        continue
                    if entry:  # 用户编辑活动结果文件不会改变已校验的暂记。
                        validate_row(directory, key, entry['row'], line=lines.get(key), fixing=work['fix'] is not None)
                        try:
                            evidence = evidence_of(directory, entry['row'], key, expected=entry['evidence_sha256'], save_to=store)
                        except APIError as error:
                            error.problems[0]['line'] = lines.get(key)
                            raise
                    elif key in rows:
                        if work['fix'] is not None and rows[key].get('status') == 'not-executed':
                            validate_row(directory, key, rows[key], line=lines[key], fixing=True)
                        entry, evidence = create_adjudication(directory, scene, rows[key], lines[key], data, work['session'],
                                                               implementation, sorted(changed), source='submit',
                                                               crash=crashes.get(key), save_evidence_to=store)
                    else:
                        continue
                    candidates[key] = (entry, evidence)
                voided = sorted(key for key, item in data['adjudications'].items() if item['status'] == 'voided')
                todo = sorted(scope - data['adjudications'].keys() - candidates.keys())
                if work['fix'] is not None and (todo or voided):
                    reject(409, 'fix-validation-required', 'results.jsonl', '提交复验须为仍有效的每条场景提供实际执行结果')
                record, summary = business_run(db, context, work, existing=existing)
                summary = loads(work['result'], {}) | summary
                adjudications = summary.get('adjudications', {})
                staged = {}
                for key, (entry, evidence) in candidates.items():
                    published = register_adjudication(db, context, work, record, prepared[key], entry, evidence,
                                                      implementation, operator)
                    adjudications[key] = published
                    staged[key] = published['seq']
                    set_adjudication(data, entry)
                registered_summary(summary, adjudications)
                if candidates:
                    data['implementation'] = implementation
                ended = now()
                progress.finish(data, ended)
                independence = independence_summary(data)
                events = work_events(context, work, data)
                for entry in data['adjudications'].values():
                    changed.update(entry.get('product_changed', []))
                summary.update(voided=voided, unadjudicated=todo, ignored_partial_line=partial, product_changed=sorted(changed),
                               browser_crashes=sorted(crashes), independence=independence, submitted_by=operator,
                               implementer=implementation['implemented_by'] or implementation['implementer'])
                summary['sessions'] = loads(work['result'], {}).get('sessions', [work['session']])
                from . import changes
                summary['changes'] = changes.read(directory)
                summary['unrestored_changes'] = changes.pending(directory)
                if summary['unrestored_changes']:
                    # 没恢复的临时改动交给人：与本轮结束在同一个业务事务里登记一条待人处理。
                    summary['unrestored_attention'] = ledger.insert_attention(
                        db, store, context.checkout.name, '前置数据',
                        changes.attention_text(run_id, summary['unrestored_changes']), by=operator, work=run_id)
                interrupted = interruptions(directory)
                if interrupted:
                    summary['interruptions'] = interrupted
                update_business(db, record, summary, events, work, implementation, independence, ended_at=ended)
                if work['fix'] is not None:
                    from . import fixes
                    fixes.after_verify(db, store, work, summary['recorded'], adjudications=adjudications)
                progress.stage(directory, data, staged, ended_at=ended)
        progress.publish(directory, data)
        return finish_submission(context, work, data, record, summary, events, ended, operator)


def finish_submission(context, work, data, record, summary, events, ended, operator):
    store, run_id = context.store, work['id']
    if work['fix'] is not None:
        from . import fixes
        fixes.after_verify_local(context, work, summary['recorded'])
    # 先保存停服影响并恢复环境，再把运行标为结束；重试只补齐本机收尾。
    with store.local_tx() as db:
        for event in events:
            db.execute('UPDATE service_events SET scenarios=? WHERE id=?', (event['scenarios'], event['id']))
        db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
    services = finish_services(context, work, reason='提交', operator=operator, crashes=True)
    if services:
        summary.update(services)
        try:
            with store.records_tx() as db:
                db.execute('UPDATE work SET services=? WHERE id=?',
                           (dumps([service_event(e) for e in services.get('service_events', [])]), record))
        except APIError as error:
            summary['services_problem'] = '；'.join(p.get('problem', '') for p in error.problems)
    summary['progress'] = progress.summary(dict(work, status='submitted'), data)
    with store.local_tx() as db:
        db.execute("UPDATE local_work SET status='submitted', ended_at=?, result=?, implementer=?, record=? WHERE id=?",
                   (ended, dumps(summary), summary.get('implementer'), record, run_id))
    return dict(ok=True, run=run_id, checkout=context.checkout.name, **summary)


def touch(context, run_id):
    if run_id is None:
        return []
    try:
        with locked_run(context, run_id) as (work, directory):
            data = progress.read(work)
            if data.get('ended_at') or (directory / progress.PENDING).exists():
                return progress.warning(data, now())
            at = now()
            data['last_activity_at'] = at
            progress.save(directory, data)
            return progress.warning(data, at)
    except APIError:
        return []


def service_event(event):
    """服务停启记录的业务部分：谁、什么时候、停启了哪个服务、停着期间执行了哪些场景。"""
    scenarios_ = event['scenarios']
    return dict(service=event['service'], action=event['action'], ok=bool(event['ok']), operator=event['operator'],
                at=event['at'], note=event.get('note'), scenarios=json.loads(scenarios_) if isinstance(scenarios_, str) else scenarios_)


def scenarios_while_stopped(events, rows):
    """每条停止事件 → 服务停着期间执行的场景（executed_at 落在停止与下一次启动或恢复之间；按秒比较，执行时间通常只精确到秒）。"""
    from .status import instant
    result = {}
    for index, event in enumerate(events):
        if event['action'] != 'stop':
            continue
        start = instant(event['at']).replace(microsecond=0)
        end = next((instant(e['at']) for e in events[index + 1:]
                    if e['service'] == event['service'] and e['action'] in ('start', 'restore')), None)
        hit = []
        for key, row in rows.items():
            try:
                moment = instant(row['executed_at'])
            except ValueError:
                continue
            if start <= moment and (end is None or moment <= end):
                hit.append(key)
        result[event['id']] = sorted(hit)
    return result


def finish_services(context, work, *, reason, operator, crashes=False):
    """运行结束（提交或放弃）时：恢复本轮停着的服务并记录；提交时另列出异常退出的服务（区分准备前与运行期间）。

    返回要并入运行摘要的字段；项目没有声明服务、本轮也没有停过服务时返回空。"""
    from . import envs
    project, checkout, store = context.project, context.checkout, context.store
    declared = project.adapter.get('services') or {}
    with store.local() as db:
        stopped_any = db.execute("SELECT 1 FROM service_events WHERE work=? AND action='stop' LIMIT 1", (work['id'],)).fetchone()
    if not declared and not stopped_any:
        return {}
    result, problems = dict(restored=[]), []
    current = None
    try:
        if stopped_any:
            result['restored'] = daemon.call(project, 'POST', '/local/env/restore', dict(
                checkout=checkout.name, run=work['id'], operator=operator, reason=reason), timeout=1800)['restored']
        if crashes and declared:
            current = daemon.call(project, 'POST', '/local/env/status', dict(checkout=checkout.name))['environments'][0]
    except daemon.DaemonUnavailable as error:
        problems.append(str(error))
        if stopped_any:
            result['restored'] = envs.withdraw(project, store, checkout, work['id'], operator=operator, reason=reason)
    except APIError as error:
        problems.append('；'.join(p.get('problem', '') for p in error.problems))
    if crashes and declared:
        try:
            prepared = {s['name']: s['state'] for s in json.loads((Path(work['dir']) / 'services.json').read_text(encoding='utf-8'))['services']}
        except (OSError, ValueError, KeyError, TypeError):
            prepared = {}
        result['crashed_services'] = [dict(service=s['name'], about=s['about'], log=s.get('log'),
                                           during_run=prepared.get(s['name']) != 'crashed')
                                      for s in (current or {}).get('services', []) if s['state'] == 'crashed']
    with store.local() as db:
        result['service_events'] = [dict(service=r['service'], action=r['action'], ok=bool(r['ok']), operator=r['operator'],
                                         at=r['at'], note=r['note'], scenarios=json.loads(r['scenarios']))
                                    for r in db.execute('SELECT * FROM service_events WHERE work=? ORDER BY id', (work['id'],))]
    if problems:
        result['services_problem'] = '；'.join(problems)
    return result


def crash_findings(context, work, prepared):
    """租约期间浏览器异常退出：受影响身份的 Web 场景 → 受阻说明。"""
    project = context.project
    holder = f"run:{work['id']}"
    with context.store.local() as db:
        rows = list(db.execute('SELECT identity, crashes FROM browser_instances'))
    crashed = {}
    for row in rows:
        for crash in json.loads(row['crashes']):
            if crash.get('holder') == holder and crash['at'] >= work['created_at']:
                from .browsers import exit_description
                text = exit_description(crash['at'], crash.get('code'), crash.get('signal'), crash['log'])
                report = crash.get('report')
                if not report:
                    from .browsers import crash_report
                    report = crash_report(datetime.fromisoformat(crash['at']).timestamp() - 30)
                if report:
                    text += f"，崩溃报告 {report}"
                crashed[row['identity']] = text
    result = {}
    for key, scene in prepared.items():
        entry = project.entries.get(scene['verify'], {})
        if web_entry(entry):
            hit = [crashed[i] for i in entry_identities(entry) if i in crashed]
            if hit:
                result[key] = hit[0]
    return result


def abandon(context, run_id, *, by=None, launch=None):
    store = context.store
    with locked_run(context, run_id, allow_ended=True) as (work, directory):
        result = loads(work['result'], {})
        continuing = (by is not None and work['status'] == 'abandoned' and result.get('abandoned_by') == by
                      and result.get('launch') == launch)
        if work['status'] != 'prepared' and not continuing:
            reject(409, 'run-not-prepared', 'run', f"运行 #{run_id} 的状态是 {work['status']}")
        operator = by or config.operator() or work['operator']
        data = progress.read(work)
        business_error = None
        record = None
        try:
            data = progress.recover(store, work)
            with store.records() as db:
                record = progress.business(db, store, run_id)
        except APIError as error:
            business_error = '；'.join(p['problem'] for p in error.problems)
        if record and record['ended_at'] and work['status'] == 'prepared':
            finish_submission(context, work, data, record['id'], loads(record['result'], {}),
                              work_events(context, work, data), record['ended_at'], operator)
            reject(409, 'run-not-prepared', 'run', f'运行 #{run_id} 已完成业务提交')
        ended = work['ended_at'] or now()
        result['abandoned_by'] = operator
        if launch is not None:
            result['launch'] = launch
        if record:
            registered = loads(record['result'], {})
            for field in ('recorded', 'not_executed'):
                if field in registered:
                    result[field] = registered[field]
        else:
            result['recorded'] = [recorded_item(item['scenario'], item['status'], item['result'], item.get('note'),
                                                item['cause'], item['suspect_defect'], item['finding'],
                                                item.get('reservation'), item.get('gap'))
                                  for item in data['adjudications'].values() if item['result'] is not None]
        progress.finish(data, ended)
        progress.save(directory, data)
        from . import changes
        result.update(changes=changes.read(directory), unrestored_changes=changes.pending(directory))
        result.update(unregistered=progress.local_verdicts(data), unadjudicated=progress.unadjudicated(work, data),
                      progress=progress.summary(dict(work, status='abandoned'), data))
        with store.local_tx() as db:
            db.execute("UPDATE local_work SET status='abandoned', ended_at=?, result=? WHERE id=?",
                       (ended, dumps(result), run_id))
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
        result.update(finish_services(context, work, reason='放弃', operator=operator))
        if record and record['ended_at'] is None:
            try:
                with store.records_tx() as db:
                    db.execute('UPDATE work SET ended_at=? WHERE id=? AND ended_at IS NULL', (ended, record['id']))
            except APIError as error:
                business_error = '；'.join(p['problem'] for p in error.problems)
        if business_error:
            result['records_problem'] = business_error
        else:
            result.pop('records_problem', None)
        with store.local_tx() as db:
            db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(result), run_id))
        return dict(ok=True, run=run_id, status='abandoned', **result)


# ---- 人工复测 ----

def mark(context, scene_key, verdict, *, by=None, defect=None, note=None):
    """测试同学（或本机操作者）对场景当前内容的复测结论：通过、不通过、阻塞或需求变更。

    复测人与缺陷链接都可不填；不填复测人时记为未署名。
    """
    from .cli import scene_board
    if verdict not in status.MANUAL:
        reject(422, 'mark-invalid', 'verdict', '复测结论只能是通过、不通过、阻塞或需求变更')
    if by is not None and (not isinstance(by, str) or len(by) > 64):
        reject(422, 'mark-invalid', 'by', '复测人姓名须为文字，最长 64 字')
    if note is not None and (not isinstance(note, str) or len(note) > 4000):
        reject(422, 'mark-invalid', 'note', '说明须为文字，最长 4000 字')
    by = (by or '').strip() or None
    note = (note or '').strip() or None
    defect = None if verdict == 'pass' else normalize_defect(defect)
    found, snapshot, derived = scene_board(context)
    try:
        key = scenarios.resolve([scene_key], found['scenarios'])[0]
    except ValueError as error:
        reject(404, 'scenario-not-found', 'scenario', str(error))
    scene = found['scenarios'][key]
    if scene.get('status') == 'draft':
        reject(409, 'draft-not-runnable', 'scenario', '草稿场景确认后才能复测')
    store = context.store
    from .checkouts import branch_of
    book = derived['book']
    # of_result：本看板当时针对的那条 Agent 结果（当前指纹上参与推导的最新一条 Agent 结果）。
    latest = next((r for r in book.select((scene['file'], scene['id'], scene['fingerprint'])).records if r['kind'] == 'agent'), None)
    with store.records_tx() as db:
        result_id = db.insert(
            'INSERT INTO results(product,seq,machine,checkout,branch,file,scenario,fingerprint,product_digest,kind,status,actual,'
            'defect,note,of_result,marked_by,executed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (store.product, db.next_seq(), store.machine['id'], context.checkout.name, branch_of(context.project, context.checkout),
             scene['file'], scene['id'], scene['fingerprint'], snapshot['product_digest'], 'manual', verdict,
             '人工复测' + status.MANUAL[verdict], defect, note, latest['id'] if latest else None, by, now()))
        guards.after_result(db, store, context.checkout.name, scene, verdict, result_id)
    return dict(ok=True, scenario=key, status=verdict, label=status.MANUAL[verdict], result=result_id,
                marked_by=by, defect=defect, note=note)
