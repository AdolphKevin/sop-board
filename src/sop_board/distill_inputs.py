"""沉淀资格和固定输入；页面说明与发起校验使用同一来源。"""
import hashlib
from importlib.resources import files
import json

from .errors import APIError
from .scenario_file import load_scenarios
from .service import canonical, fingerprint
from .merge_state import read_holds
from .statistics import calculate
from .workspace import digest, skill_files


def pending_validation(status, inputs):
    from .kernel import S
    if status != S.WAIT_VALIDATION:
        return []
    return [sid for sid, option in inputs.get('eligibility', {}).items() if not option['eligible']]


def confirmation(service, db, batch, document, scene, *, current=None, pairs=None):
    sources = document['metadata'].get('sources', [])
    if current is None:
        current = {c['id']: hashlib.sha256(canonical(c).encode()).hexdigest() for c in service.case_union(db, batch)}
    if pairs is None:
        version = service.latest_version(db, batch)
        pairs = list(db.execute('SELECT case_id,case_sha256 FROM case_pairings WHERE version=? AND scenario=?',
                               (version['id'], scene['id']))) if version else []
    # 展示层允许保留暂缓用例的旧配对；确认资格不能因此继承旧版本依据。
    if any(current.get(p['case_id']) != p['case_sha256'] for p in pairs):
        return dict(confirmed_by=[], sources=sources, drafts=[])
    placeholders = {'待确认', '未确认', '待补充', '未知', '待定', 'tbd', 'unknown'}
    people = sorted({s['confirmed_by'].strip() for s in sources
                     if s.get('confirmed_by', '').strip() and s['confirmed_by'].strip().lower() not in placeholders})
    if any(tag in {'预期待确认', '待确认', '未确认'} for tag in scene.get('tags', [])):
        return dict(confirmed_by=[], sources=sources, drafts=[])
    if people:
        return dict(confirmed_by=people, sources=sources, drafts=[])
    # 只使用当前用例版本的确认；普通上传覆盖后不能继承旧草稿的确认。
    cases = {p['case_id'] for p in pairs} | (set(scene.get('refs', [])) & current.keys())
    confirmations = []
    for case in sorted(cases):
        row = db.execute('SELECT c.*,d.output_json FROM case_versions v '
            'JOIN case_uploads u ON u.batch=v.batch AND u.number=v.upload '
            'JOIN draft_confirmations c ON c.upload=u.id '
            'JOIN draft_versions d ON d.draft=c.draft AND d.number=c.version '
            'WHERE v.batch=? AND v.case_id=? AND NOT EXISTS '
            '(SELECT 1 FROM case_versions newer WHERE newer.batch=v.batch AND newer.case_id=v.case_id AND newer.upload>v.upload)',
            (batch['id'], case)).fetchone()
        if not row:
            return dict(confirmed_by=[], sources=sources, drafts=[])
        output = json.loads(row['output_json'])
        original = next((c for c in output['cases'] if c['id'] == case), None)
        questions = json.loads(row['unanswered_questions_json'])
        if (not original or not row['confirmed_by'].strip() or row['confirmed_by'].strip().lower() in placeholders
                or any(not q.get('rule') or q['rule'] in original.get('rules', []) for q in questions)):
            return dict(confirmed_by=[], sources=sources, drafts=[])
        confirmations.append(dict(case=case, draft=row['draft'], version=row['version'],
                                  confirmed_by=row['confirmed_by'], confirmed_at=row['confirmed_at']))
    return dict(confirmed_by=sorted({c['confirmed_by'] for c in confirmations}), sources=sources, drafts=confirmations)


def eligibility(service, db, batch, document, scenes, product_digest, *, allow_stale_product=False):
    details = {s['scenario']: s for s in calculate(db, batch)['scenarios']}
    holds = read_holds(db, batch)['held']
    current = {c['id']: hashlib.sha256(canonical(c).encode()).hexdigest() for c in service.case_union(db, batch)}
    version = service.latest_version(db, batch)
    pairings = {}
    if version:
        for pair in db.execute('SELECT case_id,case_sha256,scenario FROM case_pairings WHERE version=?', (version['id'],)):
            pairings.setdefault(pair['scenario'], []).append(pair)
    result = {}
    for scene in scenes:
        sid = scene['id']
        basis = confirmation(service, db, batch, document, scene, current=current, pairs=pairings.get(sid, []))
        reasons = []
        held = [h for h in holds if sid == h['case'] or sid in h.get('scenarios', [])]
        if held:
            reasons.append('用例已更新，新内容待补充：' + '、'.join(h['case'] for h in held) + '；补齐并重新合并后才可沉淀')
        if not basis['confirmed_by']:
            reasons.append('缺少预期确认人，或草稿仍有对应的未回答问题')
        detail = details.get(sid)
        agent = detail['latest_agent'] if detail and detail['fingerprint'] == fingerprint(scene) else None
        if not agent or agent['status'] != 'pass':
            reasons.append('当前场景没有最新的 Agent 通过记录，须先经真实入口执行通过')
        elif agent['product_changed']:
            reasons.append('通过记录产生时产品在执行期间有变化，须在固定产品上重新经真实入口执行')
        elif agent['product_digest'] != product_digest and not allow_stale_product:
            reasons.append('通过记录与当前产品内容不一致，须重新经真实入口执行')
        elif not agent['evidence']:
            reasons.append('通过记录缺少真实入口证据')
        result[sid] = dict(eligible=not reasons, reasons=reasons, confirmation=basis, held=held,
                          passed=agent if agent and agent['status'] == 'pass' else None)
    return result


def freeze(service, key, name, selected, *, enforce=True):
    workspace = service.workspace
    workspace.reload()
    if not workspace.configured:
        raise ValueError('工作区配置不合格')
    path = workspace.scenario_path(name)
    raw = path.read_text(encoding='utf-8')
    document = load_scenarios(raw, entries=set(workspace.entries))
    scenes = [s for n in document['sops'] for s in n['scenarios'] if s['id'] in selected]
    if document['metadata']['batch'] != name or {s['id'] for s in scenes} != set(selected):
        raise ValueError('所选场景已移除或不属于当前批次')
    aliases = workspace.aliases(document)
    product = workspace.product(aliases)
    with service.store.connection() as db:
        batch = service.batch(db, key, name)
        version = service.latest_version(db, batch)
        if not version or version['content'] != raw:
            raise ValueError('场景原文与看板当前版本不一致，等待同步后重试')
        choices = eligibility(service, db, batch, document, scenes, product['product_digest'])
        initiation = eligibility(service, db, batch, document, scenes, product['product_digest'], allow_stale_product=product['dirty']) if enforce else choices
        problems = [dict(field=sid, problem='；'.join(choice['reasons'])) for sid, choice in initiation.items() if not choice['eligible']]
        if enforce and problems:
            raise APIError(409, 'distill-ineligible', problems)
        passes = {}
        for sid, choice in choices.items():
            passed = choice['passed']
            if not passed:
                passes[sid] = None
                continue
            row = db.execute('SELECT product_commit,product_dirty FROM rounds WHERE id=?', (passed['round_id'],)).fetchone()
            evidence = []
            for item in passed['evidence']:
                evidence_path = service.store.data / 'evidence' / item['sha256'][:2] / item['sha256']
                if not evidence_path.is_file() or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError(sid + ' 的通过证据缺失或内容变化')
                evidence.append(dict(item, path=str(evidence_path)))
            passes[sid] = dict(result_id=passed['id'], round=passed['round'], evidence=evidence,
                independent_acceptance='completed' if passed['independence'] == 'independent' else 'incomplete',
                report=dict(kind='acceptance' if passed['independence'] == 'independent' else 'self-check',
                    record=f"round:{passed['round']}:result:{passed['id']}", scenario_fingerprint=passed['fingerprint'],
                    product_commit=row['product_commit'], product_digest=passed['product_digest'], executor=passed['executor']))
    distill_skill = files('sop_board').joinpath('skills/regression-distill')
    adapter_files = {}
    for command in ('worktree', 'cleanup', 'env'):
        for argument in workspace.adapter.get(command, []):
            script = workspace.data / argument
            if script.is_file():
                adapter_files[str(script.resolve())] = hashlib.sha256(script.read_bytes()).hexdigest()
    snapshot = dict(scenario_file_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        scenarios={s['id']: fingerprint(s) for s in scenes},
        held={sid: c['held'] for sid, c in choices.items() if c['held']},
        confirmations={sid: c['confirmation'] for sid, c in choices.items()}, passes=passes,
        config_digest=workspace.config_digest, adapter_digest=digest(adapter_files), skill_digest=workspace.skill_digest(),
        distill_skill_digest=digest(skill_files(distill_skill)),
        repositories={a: dict(root=str(workspace.repos[a]), identity=workspace.identities[a]) for a in aliases},
        **product)
    inputs = dict(document=document, raw=raw, scenes=scenes, aliases=aliases,
        scenario_file=workspace.display_path(path), entries=workspace.execution_entries(),
        skill_path=str(workspace.skill), distill_skill=str(distill_skill / 'SKILL.md'),
        eligibility={sid: {key: choice[key] for key in ('eligible', 'reasons')} for sid, choice in choices.items()})
    return snapshot, inputs


INPUT_NAMES = dict(scenario_file_sha256='场景原文', scenarios='场景内容指纹', confirmations='来源或确认人', held='用例待补充状态',
        passes='通过记录或证据', config_digest='工作区配置', adapter_digest='适配脚本', skill_digest='项目验证技能',
        distill_skill_digest='回归沉淀技能', repositories='仓库身份或路径', product_digest='产品内容')


def changed(before, after, *, waiting_commit=False, waiting_validation=False):
    from .kernel import Inputs
    # 旧任务没有该字段时按无暂缓读取；新出现的暂缓仍会使旧任务作废。
    before, after = dict(before, held=before.get('held', {})), dict(after, held=after.get('held', {}))
    names = {field: label for field, label in INPUT_NAMES.items() if not (waiting_validation and field == 'passes')}
    changes = Inputs(before, names).changed(after)
    # 待提交允许提交同一份内容；待验证允许新通过记录替换旧证据，其余输入仍须不变。
    if not waiting_commit:
        changes += Inputs(before, dict(repos='仓库提交或产品内容')).changed(after)
    return '、'.join(dict.fromkeys(changes)) + '变化' if changes else ''
