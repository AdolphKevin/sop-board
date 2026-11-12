"""工作台接口：场景树与状态、场景详情（四要素、证据、历史）、证据下载、复测标注、草稿审阅（批注、同意、文件与节点面板）、本机 AI 重跑。

内网响应只含业务字段：不返回会话编号、本机路径、命令与凭据变量名（server 另做遮蔽）。
"""
import json
import re
from urllib.parse import quote

from . import checkouts as checkout_store, guards, scenarios, status
from .cli import Context, scene_board
from .errors import reject
from .evidence_types import content_type, video_response
from .server import Response, html_response, json_response, route
from .time_format import local_time

INDEPENDENCE = status.INDEPENDENCE
TRACE_VIEWER = 'https://trace.playwright.dev'


def operator_for(request):
    """本机页面以本机操作者为缺省署名；内网页面不透露它。"""
    from . import config
    return config.operator() if request.local else None


def context_for(app, name):
    app.reload()
    return Context(app.project, app.store, app.checkout(name))


def board(app, request):
    context = context_for(app, request.query.get('checkout'))
    found, snapshot, derived = scene_board(context)
    files = []
    for name, info in sorted(found['files'].items()):
        item = dict(name=name, status=info.get('status'), requirement=info.get('requirement'),
                    invalid=bool(info['problems']), nodes=info.get('nodes', []))
        if info['problems']:
            item['problems'] = [dict(field=p['field'], problem=p['problem'], line=p.get('line')) for p in info['problems'][:20]]
        files.append(item)
    items = {}
    for key, scene in found['scenarios'].items():
        info = derived['items'][key]
        items[key] = dict(state=info['state'], label=info['label'], reason=info['reason'], distill=info['distill'],
                          review=info['review'], id=scene['id'], file=scene['file'], node=scene['node'], title=scene['title'],
                          priority=scene['priority'], verify=scene['verify'], polarity=scene['polarity'], actor=scene['actor'])
        if 'draft_review' in info:
            draft = info['draft_review']
            items[key]['draft_review'] = dict(state=draft['state'], label=draft['label'], approvers=draft['approvers'])
    counts = dict(derived['counts'])
    counts['distill'] = derived['distill']
    counts['review'] = derived['review']
    with app.store.connection() as db:
        stats = status.statistics(db, context.checkout.id)
    result = dict(ok=True, project=app.project.name, local=request.local, checkout=context.checkout.name,
                  checkouts=[dict(name=c.name, kind=c.kind) for c in app.checkouts()],
                  labels=status.LABELS, counts=counts, total=derived['total'], files=files, items=items,
                  stats=dict(agent_confirm_rate=stats['agent_confirm_rate'], agent_review_coverage=stats['agent_review_coverage']),
                  product=dict(dirty=snapshot['dirty'], digest=snapshot['product_digest'][:12]),
                  configured=app.project.configured)
    if request.local:
        # 本检出的环境与各服务状态只在本机页面显示，内网不返回。
        environment = app.envs.status(context.checkout)
        result['environment'] = dict(state=environment['state'], services=[
            dict(name=s['name'], about=s['about'], state=s['state'], label=s['label'], stoppable=s['stoppable'])
            for s in environment['services']])
    return result


def evidence_items(db, result_id):
    return [dict(name=row['name'], sha256=row['sha256'], type=content_type(row['name']),
                 url=f"/evidence/{row['sha256']}/{quote(row['name'])}")
            for row in db.execute('SELECT name, sha256 FROM result_evidence WHERE result=? ORDER BY ordinal', (result_id,))]


def history_entry(db, row, *, local, stopped=None, project=None):
    item = dict(id=row['id'], kind=row['kind'], status=row['status'], executed_at=local_time(row['executed_at']),
                actual=row['actual'], finding=row['finding'], note=row['note'], defect=row['defect'])
    if row['kind'] == 'manual':
        item.update(label='人工复测' + status.MANUAL[row['status']], marked_by=row['marked_by'] or '未署名')
    else:
        label = status.LABELS.get(row['status'], row['status'])
        if row['reused_from']:
            label += '（沿用早先结果的证据，未实际执行）'
        item.update(label=label, independence=INDEPENDENCE.get(row['independence'], '独立性未声明'), run=row['work'],
                    snapshot=row['product_digest'][:12] if row['product_digest'] else None,
                    product_changed=json.loads(row['product_changed']))
        names_ = ((stopped or {}).get(row['work']) or {}).get(scenarios.key(row['file'], row['scenario']), [])
        if names_ and project is not None:
            from .envs import stopped_labels
            item['stopped_services'] = stopped_labels(project, names_)
        work = db.execute('SELECT launch FROM work WHERE id=?', (row['work'],)).fetchone() if row['work'] else None
        item['launch'] = work['launch'] if work else None
        if local:
            item.update(executor=row['executor'], implementer=row['implementer'])
    item['evidence'] = evidence_items(db, row['id'])
    return item


def detail(app, request):
    context = context_for(app, request.query.get('checkout'))
    key = request.query.get('key', '')
    found, snapshot, derived = scene_board(context)
    if key not in found['scenarios']:
        reject(404, 'scenario-not-found', 'key', f'场景不存在：{key}')
    scene, info = found['scenarios'][key], derived['items'][key]
    coverage = {'expected': {}, 'forbidden_effects': {}}
    if info['guard']['valid']:
        guard = guards.load_index(guards.index_path(context))['guards'][key]
        for part in coverage:
            coverage[part] = {item['index']: item['tests'] for item in guard['coverage'][part]}
    from .envs import stopped_during
    with app.store.connection() as db:
        rows = db.execute('SELECT * FROM results WHERE checkout=? AND file=? AND scenario=? ORDER BY id DESC',
                          (context.checkout.id, scene['file'], scene['id'])).fetchall()
        stopped = stopped_during(db, [row['work'] for row in rows])
        history = [dict(history_entry(db, row, local=request.local, stopped=stopped, project=app.project),
                        current=row['fingerprint'] == scene['fingerprint'])
                   for row in rows]
        skipped = status.latest_not_executed(status.not_executed(db, context.checkout.id), scene)
        guard_runs = [dict(status=r['status'], reason=r['reason'], ran_at=local_time(r['ran_at']), operator=r['operator'],
                           snapshot=r['product_digest'][:12],
                           evidence=[dict(name=o['name'], url=f"/evidence/{o['sha256']}/{quote(o['name'])}", type='text/plain')
                                     for o in json.loads(r['output'])])
                      for r in db.execute('SELECT * FROM guard_runs WHERE checkout=? AND file=? AND scenario=? AND fingerprint=? '
                                          'ORDER BY id DESC LIMIT 10',
                                          (context.checkout.id, scene['file'], scene['id'], scene['fingerprint']))]
    latest = next((h for h in history if h['current'] and h['kind'] == 'agent'), None)
    fields = {name: scene.get(name, []) for name in (*scenarios.CONTENT_FIELDS, 'refs', 'evidence_hints')}
    result = dict(ok=True, key=key, local=request.local, checkout=context.checkout.name,
                  scene=dict(id=scene['id'], file=scene['file'], node=scene['node'], node_title=scene['node_title'],
                             title=scene['title'], actor=scene['actor'], priority=scene['priority'], verify=scene['verify'],
                             polarity=scene['polarity'], negative_type=scene.get('negative_type'), draft=scene.get('status') == 'draft',
                             fingerprint=scene['fingerprint'], **fields),
                  state=dict(state=info['state'], label=info['label'], reason=info['reason'], distill=info['distill'],
                             review=info['review'], guard=info['guard']),
                  coverage=coverage, latest=latest, history=history, guard_runs=guard_runs, annotations=[],
                  # 最近一次未执行（谁、什么时候、缺什么）：不是结果，不改变状态，也不进历史。
                  not_executed=dict(skipped, at=local_time(skipped['at'])) if skipped else None)
    if scene.get('status') == 'draft':
        result.update(draft_detail(app, context, scene, info['draft_review']))
    return result


def note_entry(note):
    return dict(id=note['id'], target=note['target'], text=note['text'], author=note['author'], status=note['status'],
                note=note['note'], created_at=local_time(note['created_at']),
                resolved_at=local_time(note['resolved_at']) if note['resolved_at'] else None)


def draft_detail(app, context, scene, review):
    """草稿场景详情：审阅状态与同意记录、refs 指向的 SOP 规则原文、检查清单、本场景与整个文件的批注。"""
    from . import authoring
    notes = [note_entry(a) for a in authoring.annotations(app.store, file=scene['file'], include_resolved=True)
             if a['target'] in (scene['id'], 'file')]
    return dict(annotations=notes, checklist=list(authoring.CHECKLIST), rules=authoring.rule_texts(context, scene.get('refs')),
                draft=dict(state=review['state'], label=review['label'], approvers=review['approvers'],
                           approvals=authoring.approval_history(app.store, scene['file'], scene['id'], scene['fingerprint'])))


@route('GET', '/', 'lan')
def home(app, request):
    return Response(302, b'', 'text/plain; charset=utf-8', [('Location', f'/p/{quote(app.project.name)}')])


@route('GET', r'/p/([^/]+)', 'lan')
def workbench(app, request):
    if request.arg(1) != app.project.name:
        reject(404, 'project-not-found', 'project', '项目不存在')
    from .pages import workbench as page
    return html_response(page(app.project.name, local=request.local, operator=operator_for(request)))


@route('GET', '/api/board', 'lan')
def board_api(app, request):
    return json_response(board(app, request))


@route('GET', '/api/scenario', 'lan')
def scenario_api(app, request):
    return json_response(detail(app, request))


@route('GET', r'/evidence/([0-9a-f]{64})/([^/]+)', 'lan')
def evidence(app, request):
    sha, name = request.arg(1), request.arg(2)
    with app.store.connection() as db:
        known = db.execute('SELECT 1 FROM result_evidence WHERE sha256=? LIMIT 1', (sha,)).fetchone()
    if not known or not app.store.evidence_path(sha).is_file():
        reject(404, 'evidence-not-found', 'evidence', '证据不存在')
    raw = app.store.read_evidence(sha)
    kind = content_type(name)
    headers = [('Content-Disposition', "inline; filename*=UTF-8''" + quote(name))]
    if kind.startswith('video/'):
        code, body, extra = video_response(raw, request.headers.get('Range'))
        return Response(code, body, kind, headers + extra)
    if name.endswith('.zip') and 'trace' in name:
        headers.append(('Access-Control-Allow-Origin', TRACE_VIEWER))
    return Response(200, raw, kind, headers)


@route('POST', '/api/mark', 'lan')
def mark_api(app, request):
    from . import config, runs
    context = context_for(app, request.json_field('checkout'))
    by = request.json_field('by')
    if request.local and not (isinstance(by, str) and by.strip()):
        by = config.operator()
    return json_response(runs.mark(context, request.json_field('scenario') or '', request.json_field('verdict'),
                                   by=by, defect=request.json_field('defect'), note=request.json_field('note')))


@route('POST', '/api/launch', 'local')
def launch_api(app, request):
    context = context_for(app, request.json_field('checkout'))
    key = request.json_field('scenario')
    if not isinstance(key, str) or not key:
        reject(422, 'scenario-required', 'scenario', '须给出场景')
    from . import config
    return json_response(dict(ok=True, launch=app.launcher.start(
        context.checkout, 'run', [key], requested_by=(config.operator() or '本机') + '（工作台）')))


@route('GET', '/api/runs', 'lan')
def runs_api(app, request):
    from .launcher import public
    context = context_for(app, request.query.get('checkout'))
    with app.store.connection() as db:
        launches = [public(row, [dict(r) for r in db.execute('SELECT id, kind, status, session FROM work WHERE launch=?', (row['id'],))],
                           local=request.local)
                    for row in db.execute('SELECT * FROM launches WHERE checkout=? ORDER BY id DESC LIMIT 50', (context.checkout.id,))]
        works = []
        for row in db.execute("SELECT * FROM work WHERE checkout=? AND kind IN ('run','distill') ORDER BY id DESC LIMIT 50",
                              (context.checkout.id,)):
            item = dict(id=row['id'], kind=row['kind'], status=row['status'], launch=row['launch'], fix=row['fix'],
                        created_at=local_time(row['created_at']), ended_at=local_time(row['ended_at']) if row['ended_at'] else None,
                        scope=[s['scenario'] for s in json.loads(row['scope'])])
            summary = json.loads(row['result']) if row['result'] else {}
            item['recorded'] = len(summary.get('recorded', []))
            item['voided'] = summary.get('voided', [])
            item['not_executed'] = [n['scenario'] for n in summary.get('not_executed', [])]
            if request.local:
                item.update(session=row['session'], operator=row['operator'], dir=row['dir'])
                item['service_events'] = [dict(service=e['service'], action=e['action'], ok=bool(e['ok']), operator=e['operator'],
                                               at=local_time(e['at']), note=e['note'], scenarios=json.loads(e['scenarios']))
                                          for e in db.execute('SELECT * FROM service_events WHERE work=? ORDER BY id', (row['id'],))]
            works.append(item)
    for launch in launches:
        for key in ('started_at', 'ended_at'):
            if launch.get(key):
                launch[key] = local_time(launch[key])
    return json_response(dict(ok=True, local=request.local, checkout=context.checkout.name, launches=launches, runs=works))


@route('GET', r'/api/launches/([0-9]+)/log', 'local')
def launch_log(app, request):
    with app.store.connection() as db:
        row = db.execute('SELECT log FROM launches WHERE id=?', (int(request.arg(1)),)).fetchone()
    if row is None or not row['log']:
        reject(404, 'launch-not-found', 'launch', '启动不存在')
    from pathlib import Path
    path = Path(row['log'])
    raw = path.read_bytes()[-512 * 1024:] if path.is_file() else b''
    return Response(200, raw, 'text/plain; charset=utf-8')


@route('GET', r'/p/([^/]+)/runs', 'lan')
def runs_page(app, request):
    from .pages import runs as page
    return html_response(page(app.project.name, local=request.local, operator=operator_for(request)))


@route('GET', r'/p/([^/]+)/report', 'lan')
def report_page(app, request):
    from . import reports
    from .pages import report as page
    context = context_for(app, request.query.get('checkout'))
    markdown, evidence = reports.build(context, file=request.query.get('file') or None, local=request.local)
    if request.query.get('download'):
        return Response(200, markdown.encode(), 'text/markdown; charset=utf-8',
                        [('Content-Disposition', f"attachment; filename*=UTF-8''{quote(context.checkout.name)}-report.md")])
    return html_response(page(app.project.name, context.checkout.name, markdown, evidence, local=request.local, operator=operator_for(request)))


@route('GET', r'/p/([^/]+)/attention', 'lan')
def attention_page(app, request):
    from . import ledger
    from .pages import attention as page
    context = context_for(app, request.query.get('checkout'))
    with app.store.connection() as db:
        items = ledger.open_attention(db, context.checkout.id, local=request.local)
    return html_response(page(app.project.name, context.checkout.name, items, local=request.local, operator=operator_for(request)))


def attach(app):
    pass


# ---- 收件箱、答复、草稿审阅、SOP 原文、交互地图 ----

@route('GET', r'/p/([^/]+)/inbox', 'lan')
def inbox_page(app, request):
    from . import authoring
    from .pages import inbox as page
    with app.store.connection() as db:
        items = authoring.inbox_rows(db, status='pending')
    questions = authoring.answers(app.store)
    return html_response(page(app.project.name, items, questions, local=request.local, operator=operator_for(request)))


@route('POST', '/api/inbox', 'lan')
def inbox_api(app, request):
    from . import authoring
    by = request.json_field('by')
    if not isinstance(by, str) or not by.strip() or len(by) > 64:
        reject(422, 'uploader-required', 'by', '请填写上传人姓名')
    return json_response(authoring.inbox_add(app.store, request.json_field('kind') or 'cases', request.json_field('filename'),
                                             request.json_field('content'), by=by.strip()))


@route('POST', '/api/answer', 'lan')
def answer_api(app, request):
    from . import authoring
    question = request.json_field('question')
    if type(question) is not int:
        reject(422, 'answer-invalid', 'question', '须给出问题编号')
    return json_response(authoring.answer(app.store, question, request.json_field('answer'), by=request.json_field('by')))


@route('POST', '/api/annotate', 'lan')
def annotate_api(app, request):
    from . import authoring
    context = context_for(app, request.json_field('checkout'))
    return json_response(authoring.annotate(context, request.json_field('file'), request.json_field('target') or 'file',
                                            request.json_field('text'), by=signer_for(request)))


@route('GET', '/api/annotations', 'lan')
def annotations_api(app, request):
    from . import authoring
    items = authoring.annotations(app.store, file=request.query.get('file'), include_resolved=True)
    target = request.query.get('target')
    return json_response(dict(ok=True, annotations=[a for a in items if target is None or a['target'] == target]))


# 草稿只在工作台“草稿”筛选中审阅：场景详情、节点面板与文件面板。内网可以批注、同意或撤销自己的同意（须署名）。

def signer_for(request):
    """内网须署名；本机不填时以本机操作者署名。"""
    from . import config
    by = request.json_field('by')
    if request.local and not (isinstance(by, str) and by.strip()):
        by = config.operator()
    return by


@route('GET', '/api/draft', 'lan')
def draft_api(app, request):
    """文件面板与节点面板：需求说明与来源、审阅进度、各节点场景的四要素与审阅状态、文件级批注、能否确认。"""
    from . import authoring
    context = context_for(app, request.query.get('checkout'))
    name = request.query.get('file', '')
    found = authoring.draft_scan(context)
    info = authoring.draft_info(found, name)
    review = authoring.review_files(context, name)[0]
    states = {item['scenario']: item for item in review['scenarios']}
    nodes = []
    for node in info['nodes']:
        rows = []
        for sid in node['scenarios']:
            scene = found['scenarios'][scenarios.key(name, sid)]
            state = states[sid]
            rows.append(dict({field: scene.get(field) for field in (
                'id', 'title', 'actor', 'priority', 'verify', 'polarity', 'negative_type', *scenarios.CONTENT_FIELDS)},
                key=scenarios.key(name, sid), fingerprint=scene['fingerprint'], review=state['review'], label=state['label'],
                approvers=state['approvers'],
                approvals=[dict(id=a['id'], by=a['by'], at=local_time(a['at'])) for a in state['approvals']]))
        nodes.append(dict(id=node['id'], title=node['title'], actor=node['actor'], entry=node['entry'], scenarios=rows))
    notes = [note_entry(a) for a in authoring.annotations(app.store, file=name, include_resolved=True) if a['target'] == 'file']
    result = dict(ok=True, local=request.local, checkout=context.checkout.name, file=name, requirement=info.get('requirement'),
                  version=info.get('version'), sources=info.get('sources'), progress=review['progress'], total=review['total'],
                  open_annotations=review['open_annotations'], ready=review['ready'], nodes=nodes, annotations=notes)
    if request.local:
        result['command'] = f'sop-board draft confirm {name}'
    return json_response(result)


@route('POST', '/api/approve', 'lan')
def approve_api(app, request):
    """同意一条草稿场景（scenario），或同意一个节点中的全部待审场景（node）；fingerprints 为页面上看到的指纹。"""
    from . import authoring
    context = context_for(app, request.json_field('checkout'))
    scenario, node = request.json_field('scenario'), request.json_field('node')
    if node is not None and not isinstance(node, str):
        reject(422, 'approval-invalid', 'node', '节点编号应为文字')
    if node is None and not (isinstance(scenario, str) and scenario):
        reject(422, 'approval-invalid', 'scenario', '须给出场景编号或节点编号')
    fingerprints = request.json_field('fingerprints') or {}
    if request.json_field('fingerprint') and scenario:
        fingerprints = {scenario: request.json_field('fingerprint')}
    return json_response(authoring.approve(context, request.json_field('file') or '', [scenario] if node is None else [],
                                           by=signer_for(request), fingerprints=fingerprints, node=node))


@route('POST', '/api/unapprove', 'lan')
def unapprove_api(app, request):
    """撤销自己的同意：approval 为同意编号，或给出 file 与 scenario。"""
    from . import authoring
    context = context_for(app, request.json_field('checkout'))
    return json_response(authoring.revoke_approval(context, by=signer_for(request), approval=request.json_field('approval'),
                                                   file=request.json_field('file'), scenario=request.json_field('scenario')))


@route('GET', r'/p/([^/]+)/sop', 'lan')
def sop_page(app, request):
    from .pages import sop as page
    return html_response(page(app.project.name, local=request.local, operator=operator_for(request)))


@route('GET', '/api/sop', 'lan')
def sop_api(app, request):
    """场景目录下的 Markdown（业务 SOP 与待确认问题），按 1～3 级标题分节渲染，供左树右文展示。"""
    from . import authoring
    from .sop_markdown import sections
    context = context_for(app, request.query.get('checkout'))
    documents = []
    for path in authoring.sop_files(context):
        try:
            raw = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as error:
            documents.append(dict(name=path.name, problem=f'无法读取：{error}', sections=[]))
            continue
        documents.append(dict(name=path.name, sections=sections(raw, path.name)))
    return json_response(dict(ok=True, checkout=context.checkout.name,
                              checkouts=[dict(name=c.name, kind=c.kind) for c in app.checkouts()], documents=documents))


@route('GET', r'/p/([^/]+)/atlas', 'lan')
def atlas_page(app, request):
    from . import atlas
    from .pages import atlas as page
    context = context_for(app, request.query.get('checkout'))
    return html_response(page(app.project.name, context.checkout.name, atlas.summary(context), local=request.local, operator=operator_for(request)))
