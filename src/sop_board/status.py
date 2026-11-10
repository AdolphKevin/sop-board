"""按检出推导场景状态与统计；状态不存储，每次由结果、守护运行与修复记录算出。

- not-run 未执行：当前指纹没有结果。
- pass 通过：最新结果为通过，且产品快照等于当前快照、执行期间产品没有变化；
  或最新结果是不绑定产品快照（product_digest 为空）的导入结果，产品变化不使它转待复测。
- guarded 守护通过：曾经真实入口通过，当前快照上守护测试全部通过。
- retest 待复测：通过发生在旧快照上；或不通过的场景修复已关闭。
- fail 失败 / blocked 受阻 / skip 跳过：最新结果如此（人工不通过按失败，人工阻塞按受阻）。
- changed 需求变更：测试同学复测时标为需求变更；场景文件按新需求修改后指纹变化，回到未执行。
另有两个徽标，可与状态并存：
- distill 待沉淀：同一指纹上已有独立真实入口通过，但没有有效守护。
- review 待人工复核：状态为通过，且最新结果是 Agent 独立真实入口通过，之后还没有人工标注。
"""
import json

LABELS = {'not-run': '未执行', 'pass': '通过', 'guarded': '守护通过', 'retest': '待复测',
          'fail': '失败', 'blocked': '受阻', 'changed': '需求变更', 'skip': '跳过', 'draft': '草稿'}
ORDER = ('retest', 'fail', 'blocked', 'changed', 'not-run', 'pass', 'guarded', 'skip', 'draft')
MANUAL = {'pass': '通过', 'fail': '不通过', 'blocked': '阻塞', 'changed': '需求变更'}
UNBOUND = '导入的历史结果，不绑定产品快照'


def manual_reason(row):
    reason = '测试同学复测' + MANUAL[row['status']]
    return reason + ('：' + row['note'] if row.get('note') else '')
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


def guard_runs(db, checkout_id, product_digest):
    """当前快照上每个场景最近一次守护运行。"""
    return {(row['file'], row['scenario'], row['fingerprint']): dict(row) for row in db.execute(
        'SELECT * FROM guard_runs WHERE checkout=? AND product_digest=? ORDER BY id', (checkout_id, product_digest))}


def closed_fixes(db, checkout_id):
    """已关闭的修复：场景键 → 关闭时间。"""
    result = {}
    for row in db.execute("SELECT problems, ended_at FROM fixes WHERE checkout=? AND status='closed'", (checkout_id,)):
        for problem in json.loads(row['problems']):
            key = (problem['file'], problem['scenario'])
            result[key] = max(result.get(key, ''), row['ended_at'] or '')
    return result


def derive(scene, results, product_digest, *, guard=None, guard_valid=False, guard_reason='', guard_failure=None, fixed_at=None):
    """单个场景的状态。results 为该场景当前指纹的结果（新到旧）。"""
    if scene.get('status') == 'draft':
        return dict(state='draft', reason='草稿，确认后才进入执行')
    if not results:
        return dict(state='not-run', reason='当前内容还没有结果')
    latest = results[0]
    if fixed_at:
        manual = [r for r in results if r['kind'] == 'manual']
        failed_before = any(r['status'] == 'fail' and r['executed_at'] < fixed_at for r in manual)
        passed_after = any(r['status'] == 'pass' and r['executed_at'] > fixed_at for r in manual)
        if failed_before and not passed_after and not (latest['kind'] == 'manual' and latest['executed_at'] > fixed_at):
            return dict(state='retest', reason='修复已关闭，等待测试同学复测', result=latest['id'])
    if latest['status'] == 'changed':
        return dict(state='changed', reason=manual_reason(latest) + '；按新需求修改场景文件后回到未执行', result=latest['id'])
    if latest['status'] in ('fail', 'blocked'):
        if fixed_at and fixed_at > latest['executed_at']:
            return dict(state='retest', reason='修复已关闭，等待复测', result=latest['id'])
        reason = manual_reason(latest) if latest['kind'] == 'manual' else latest.get('finding') or ''
        return dict(state=latest['status'], reason=reason, result=latest['id'])
    if latest['status'] == 'skip':
        return dict(state='skip', reason=latest.get('finding') or '', result=latest['id'])
    changed = json.loads(latest['product_changed'] or '[]')
    if latest['product_digest'] is None and not changed:
        # 从旧看板导入的历史结果不绑定产品快照：保持通过，直到该场景有新的执行或人工结论。
        return dict(state='pass', reason=UNBOUND, result=latest['id'])
    if latest['product_digest'] == product_digest and not changed:
        return dict(state='pass', reason='', result=latest['id'])
    if guard and guard_valid:
        return dict(state='guarded', reason='守护测试在当前产品上通过', result=latest['id'], guard_run=guard['id'])
    if changed:
        reason = '执行期间产品有变化（' + '、'.join(changed[:3]) + ('等' if len(changed) > 3 else '') + '）'
    elif guard_failure:
        reason = '守护测试未通过：' + (guard_failure['reason'] or '')
    elif guard_valid:
        reason = '产品已变化，守护测试尚未在当前产品上运行'
    elif guard_reason and guard_reason != '没有守护':
        reason = '产品已变化，守护无效：' + guard_reason
    else:
        reason = '产品已变化，守护未覆盖'
    return dict(state='retest', reason=reason, result=latest['id'])


def accepted(row):
    """经真实入口的 Agent 独立通过：不是保留通过，执行期间产品无变化。"""
    return (row['kind'] == 'agent' and row['status'] == 'pass' and row['independence'] == 'independent'
            and not row['reused_from'] and not json.loads(row['product_changed'] or '[]'))


def independent_pass(results):
    """同一指纹上经真实入口的独立通过（执行期间产品无变化）。"""
    return next((row for row in results if accepted(row)), None)


def board(db, checkout_id, scenes, product_digest, *, guard_validity=None):
    """全部场景的状态与计数。guard_validity：场景键 → (有效, 原因)。"""
    grouped = latest_results(db, checkout_id)
    runs = guard_runs(db, checkout_id, product_digest)
    fixes = closed_fixes(db, checkout_id)
    guard_validity = guard_validity or {}
    items, counts = {}, {state: 0 for state in ORDER}
    distill = review = 0
    for scene_key, scene in scenes.items():
        identity = (scene['file'], scene['id'], scene['fingerprint'])
        results = grouped.get(identity, [])
        valid, guard_reason = guard_validity.get(scene_key, (False, '没有守护'))
        run = runs.get(identity)
        info = derive(scene, results, product_digest, guard=run if run and run['status'] == 'pass' else None,
                      guard_valid=valid, guard_reason=guard_reason,
                      guard_failure=run if run and run['status'] == 'fail' else None,
                      fixed_at=fixes.get((scene['file'], scene['id'])))
        info['label'] = LABELS[info['state']]
        info['distill'] = bool(scene.get('status') != 'draft' and independent_pass(results) and not valid)
        # 人工复核须与 Agent 看同一产品快照：状态离开通过（产品变化）后不再待复核。
        info['review'] = info['state'] == 'pass' and accepted(results[0])
        info['guard'] = dict(valid=valid, reason=guard_reason)
        counts[info['state']] += 1
        distill += info['distill']
        review += info['review']
        items[scene_key] = info
    return dict(items=items, counts=counts, distill=distill, review=review, total=len(scenes))


def confirmation(rows):
    """Agent 独立通过的人工复核：样本 → 同一快照上关联它的最近一条人工通过或不通过（不绑定快照的导入样本不限快照）。

    只有阻塞或需求变更的样本记为未决（取最近一条），不进分母。
    """
    samples = {row['id']: row for row in rows if accepted(row)}
    verdicts, undecided = {}, {}
    for row in rows:
        sample = samples.get(row['of_result']) if row['kind'] == 'manual' else None
        # 不绑定快照的导入样本，任何快照上关联它的人工结论都算配对。
        if sample is None or (sample['product_digest'] is not None and row['product_digest'] != sample['product_digest']):
            continue
        (verdicts if row['status'] in ('pass', 'fail') else undecided)[sample['id']] = row
    undecided = {k: v for k, v in undecided.items() if k not in verdicts}
    misjudgments = [dict(scenario=f"{samples[k]['file']}#{samples[k]['scenario']}", fingerprint=samples[k]['fingerprint'],
                         result=k, run=samples[k]['work'], executor=samples[k]['executor'], agent_at=samples[k]['executed_at'],
                         mark=m['id'], marked_at=m['executed_at'], marked_by=m['marked_by'], defect=m['defect'], note=m['note'])
                    for k, m in sorted(verdicts.items(), key=lambda item: item[1]['id']) if m['status'] == 'fail']
    counts = {}
    for row in undecided.values():
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return dict(samples=len(samples), reviewed=len(verdicts),
                confirmed=sum(m['status'] == 'pass' for m in verdicts.values()),
                undecided=counts, misjudgments=misjudgments)


def statistics(db, checkout_id):
    """Agent 通过率、一次通过率、人工复测覆盖、人工确认率与独立性计数。

    前四项按各指纹最新结果；人工确认率按全部 Agent 独立通过样本（见 confirmation）。
    """
    rows = [dict(row) for row in db.execute("SELECT * FROM results WHERE checkout=? ORDER BY id", (checkout_id,))]
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
    review = confirmation(rows)
    return dict(agent_pass_rate=ratio(sum(r['status'] == 'pass' for r in decided), len(decided)),
                first_pass_rate=ratio(sum(r['status'] == 'pass' for r in first_decided), len(first_decided)),
                manual_coverage=ratio(sum(1 for k in latest_agent if k in manual), len(latest_agent)),
                manual_pass_rate=ratio(sum(r['status'] == 'pass' for r in manual.values()), len(manual)),
                agent_confirm_rate=ratio(review['confirmed'], review['reviewed']),
                agent_review_coverage=ratio(review['reviewed'], review['samples']),
                review_undecided=review['undecided'], misjudgments=review['misjudgments'],
                independence=independence_counts)
