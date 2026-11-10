"""按检出推导场景状态与统计；状态不存储，每次由结果、守护运行与修复记录算出。

- not-run 未执行：当前指纹没有结果。
- pass 通过：最新结果为通过，且产品快照等于当前快照、执行期间产品没有变化。
- guarded 守护通过：曾经真实入口通过，当前快照上守护测试全部通过。
- retest 待复测：通过发生在旧快照上；或不通过的场景修复已关闭。
- fail 失败 / blocked 受阻 / skip 跳过：最新结果如此（人工不通过按失败）。
另有 distill（待沉淀）标记：同一指纹上已有独立真实入口通过，但没有有效守护。
"""
import json

LABELS = {'not-run': '未执行', 'pass': '通过', 'guarded': '守护通过', 'retest': '待复测',
          'fail': '失败', 'blocked': '受阻', 'skip': '跳过', 'draft': '草稿'}
ORDER = ('retest', 'fail', 'blocked', 'not-run', 'pass', 'guarded', 'skip', 'draft')
INDEPENDENCE = {'independent': '独立验收', 'self-check': '实现者自检', 'undeclared': '独立性未声明'}


def independence(executor, implementer):
    if not implementer:
        return 'undeclared'
    return 'self-check' if executor == implementer else 'independent'


def latest_results(db, checkout_id):
    """每个 (文件, 场景, 指纹) 的结果，按时间从新到旧。"""
    grouped = {}
    for row in db.execute('SELECT * FROM results WHERE checkout=? ORDER BY id DESC', (checkout_id,)):
        grouped.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
    return grouped


def guard_passes(db, checkout_id, product_digest):
    return {(row['file'], row['scenario'], row['fingerprint']): dict(row) for row in db.execute(
        "SELECT * FROM guard_runs WHERE checkout=? AND product_digest=? AND status='pass' ORDER BY id",
        (checkout_id, product_digest))}


def closed_fixes(db, checkout_id):
    """已关闭的修复：场景键 → 关闭时间。"""
    result = {}
    for row in db.execute("SELECT problems, ended_at FROM fixes WHERE checkout=? AND status='closed'", (checkout_id,)):
        for problem in json.loads(row['problems']):
            key = (problem['file'], problem['scenario'])
            result[key] = max(result.get(key, ''), row['ended_at'] or '')
    return result


def derive(scene, results, product_digest, *, guard=None, guard_valid=False, fixed_at=None):
    """单个场景的状态。results 为该场景当前指纹的结果（新到旧）。"""
    if scene.get('status') == 'draft':
        return dict(state='draft', reason='草稿，确认后才进入执行')
    if not results:
        return dict(state='not-run', reason='当前内容还没有结果')
    latest = results[0]
    if latest['status'] in ('fail', 'blocked'):
        if fixed_at and fixed_at > latest['executed_at']:
            return dict(state='retest', reason='修复已关闭，等待复测', result=latest['id'])
        reason = latest.get('finding') or ('人工复测不通过' if latest['kind'] == 'manual' else '')
        return dict(state=latest['status'], reason=reason, result=latest['id'])
    if latest['status'] == 'skip':
        return dict(state='skip', reason=latest.get('finding') or '', result=latest['id'])
    changed = json.loads(latest['product_changed'] or '[]')
    if latest['product_digest'] == product_digest and not changed:
        return dict(state='pass', reason='', result=latest['id'])
    if guard and guard_valid:
        return dict(state='guarded', reason='守护测试在当前产品上通过', result=latest['id'], guard_run=guard['id'])
    if changed:
        reason = '执行期间产品有变化（' + '、'.join(changed[:3]) + ('等' if len(changed) > 3 else '') + '）'
    else:
        reason = '产品已变化，守护测试未覆盖' if not guard_valid else '产品已变化，守护测试尚未在当前产品上运行'
    return dict(state='retest', reason=reason, result=latest['id'])


def independent_pass(results):
    """同一指纹上经真实入口的独立通过（执行期间产品无变化）。"""
    for row in results:
        if (row['kind'] == 'agent' and row['status'] == 'pass' and row['independence'] == 'independent'
                and not row['reused_from'] and not json.loads(row['product_changed'] or '[]')):
            return row
    return None


def board(db, checkout_id, scenes, product_digest, *, guard_validity=None):
    """全部场景的状态与计数。guard_validity：场景键 → (有效, 原因)。"""
    grouped = latest_results(db, checkout_id)
    guards = guard_passes(db, checkout_id, product_digest)
    fixes = closed_fixes(db, checkout_id)
    guard_validity = guard_validity or {}
    items, counts = {}, {state: 0 for state in ORDER}
    distill = 0
    for scene_key, scene in scenes.items():
        identity = (scene['file'], scene['id'], scene['fingerprint'])
        results = grouped.get(identity, [])
        valid, guard_reason = guard_validity.get(scene_key, (False, '没有守护'))
        info = derive(scene, results, product_digest, guard=guards.get(identity), guard_valid=valid,
                      fixed_at=fixes.get((scene['file'], scene['id'])))
        info['label'] = LABELS[info['state']]
        info['distill'] = bool(scene.get('status') != 'draft' and independent_pass(results) and not valid)
        info['guard'] = dict(valid=valid, reason=guard_reason)
        counts[info['state']] += 1
        distill += info['distill']
        items[scene_key] = info
    return dict(items=items, counts=counts, distill=distill, total=len(scenes))


def statistics(db, checkout_id):
    """Agent 通过率、一次通过率、人工复测覆盖与独立性计数（按当前各场景最新结果）。"""
    rows = list(db.execute("SELECT * FROM results WHERE checkout=? ORDER BY id", (checkout_id,)))
    first, latest_agent, manual = {}, {}, {}
    for row in rows:
        identity = (row['file'], row['scenario'], row['fingerprint'])
        if row['kind'] == 'agent':
            first.setdefault(identity, row)
            latest_agent[identity] = row
        else:
            manual[identity] = row
    def ratio(numerator, denominator):
        return dict(numerator=numerator, denominator=denominator,
                    value=round(numerator / denominator, 4) if denominator else None)
    decided = [r for r in latest_agent.values() if r['status'] in ('pass', 'fail')]
    first_decided = [r for r in first.values() if r['status'] in ('pass', 'fail')]
    independence_counts = {}
    for row in latest_agent.values():
        independence_counts[row['independence'] or 'undeclared'] = independence_counts.get(row['independence'] or 'undeclared', 0) + 1
    return dict(agent_pass_rate=ratio(sum(r['status'] == 'pass' for r in decided), len(decided)),
                first_pass_rate=ratio(sum(r['status'] == 'pass' for r in first_decided), len(first_decided)),
                manual_coverage=ratio(sum(1 for k in latest_agent if k in manual), len(latest_agent)),
                manual_pass_rate=ratio(sum(r['status'] == 'pass' for r in manual.values()), len(manual)),
                independence=independence_counts)
