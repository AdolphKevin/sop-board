"""按检出推导场景状态与统计；状态不存储，每次由业务记录库里的记录算出（已定决策 21）。

记录分两类，都限于场景的当前指纹：
- 结论（Agent 的通过与失败、人工标注、修复关闭）在产品内共享，按分支归属。本分支优先：当前指纹在本分支上有结论时
  只用本分支的；一条都没有（或检出不在任何分支上）时用全部分支的，并标出来源分支。
- 非结论（Agent 的受阻与跳过、守护未通过）只取本机本检出产出的；别的机器与检出的显示在“其他机器上的情况”。
选出的记录按业务记录库分配的序号（seq）排先后，执行时间只用于显示：
- not-run 未执行：没有选出任何结果。
- pass 通过：最新结果为通过，且没有更晚的本检出守护运行未通过。代码改动不改变状态。
- retest 待复测：本检出最近一次守护运行未通过且晚于最新结果；或最新为失败、受阻，之后修复已关闭；或测试同学标注不通过后修复已关闭。
- fail 失败 / blocked 受阻 / skip 跳过：最新结果如此（人工不通过按失败，人工阻塞按受阻）。
- changed 需求变更：测试同学复测时标为需求变更；场景文件按新需求修改后指纹变化，回到未执行。
另有两个徽标，可与状态并存：
- distill 待沉淀：状态为通过，选出的记录中有经真实入口的 Agent 独立通过，但没有有效守护（别人已沉淀而本机未同步、或本机无法运行时不算）。
- review 待人工复核：状态为通过，且最新结果是经真实入口的 Agent 独立通过，之后还没有人工标注。
未执行记录（运行记录里的未执行清单）不参与推导，只显示在场景详情里。
草稿场景另有审阅状态（draft_review）：annotated 有批注 / approved 已同意 / pending 待审；同意按 产品 + 文件 + 场景 + 指纹 记录。
"""
from collections import namedtuple
from datetime import datetime, timezone
import json

LABELS = {'not-run': '未执行', 'pass': '通过', 'retest': '待复测',
          'fail': '失败', 'blocked': '受阻', 'changed': '需求变更', 'skip': '跳过', 'draft': '草稿'}
ORDER = ('retest', 'fail', 'blocked', 'changed', 'not-run', 'pass', 'skip', 'draft')
MANUAL = {'pass': '通过', 'fail': '不通过', 'blocked': '阻塞', 'changed': '需求变更'}
DRAFT_REVIEW = {'pending': '待审', 'annotated': '有批注', 'approved': '已同意'}
INDEPENDENCE = {'independent': '独立验收', 'self-check': '实现者自检', 'undeclared': '独立性未声明'}
ELSEWHERE = {'blocked': '受阻', 'skip': '跳过', 'not-executed': '未执行', 'guard-failed': '守护未通过'}
NO_BRANCH = '（无分支）'

# 看板的视角：哪个产品、哪台机器、哪个检出、检出的场景仓库在哪个分支上（None 表示不在任何分支上）。
Viewer = namedtuple('Viewer', 'product machine checkout branch')


def viewer(context):
    from .checkouts import branch_of
    store = context.store
    return Viewer(store.product, store.machine['id'], context.checkout.name, branch_of(context.project, context.checkout))


def instant(value):
    """带时区的 ISO 8601 → 可比较的时刻；没有时区的按 UTC。只用于执行时间的比较，状态推导比较序号。"""
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def manual_reason(row):
    reason = '测试同学复测' + MANUAL[row['status']]
    return reason + ('：' + row['note'] if row.get('note') else '')


def independence(executor, implementer):
    if not implementer:
        return 'undeclared'
    return 'self-check' if executor == implementer else 'independent'


def conclusion(row):
    """结论：Agent 的通过与失败、全部人工标注。Agent 的受阻与跳过是非结论。"""
    return row['kind'] == 'manual' or row['status'] in ('pass', 'fail')


def branch_label(value):
    return value or NO_BRANCH


def accepted(row):
    """经真实入口的 Agent 独立通过。带 reused_from 的是早先的保留通过（未实际执行），不算；
    执行期间产品有变化的通过照常算，变化的路径只作版本记录。"""
    return (row['kind'] == 'agent' and row['status'] == 'pass' and row['independence'] == 'independent'
            and not row['reused_from'])


def independent_pass(results):
    """同一指纹上经真实入口的 Agent 独立通过（新到旧的第一条）。"""
    return next((row for row in results if accepted(row)), None)


class Selection:
    """一组（文件 + 场景 + 指纹）里参与本看板推导的记录。"""

    def __init__(self, records, fixed_seq, fallback):
        self.records = records          # 选出的结论与本检出的非结论，按序号新到旧
        self.fixed_seq = fixed_seq      # 选出的修复关闭中最晚的序号
        self.fallback = fallback        # 本分支没有结论，用了全部分支的结论


class Book:
    """一次读出产品的业务记录，供看板、命令与报告按同一套规则推导。db 是业务记录库。"""

    def __init__(self, db, view):
        self.viewer = view
        self.current = None
        self.names = {row['id']: row['name'] for row in db.execute('SELECT id, name FROM machines')}
        self.groups = {}
        for row in db.execute('SELECT * FROM results WHERE product=? ORDER BY seq DESC', (view.product,)):
            self.groups.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
        self.guards = {}
        for row in db.execute('SELECT * FROM guard_runs WHERE product=? ORDER BY seq DESC', (view.product,)):
            self.guards.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
        self.fixes = {}
        for row in db.execute("SELECT id, machine, checkout, branch, seq, problems, ended_at FROM fixes "
                              "WHERE product=? AND status='closed'", (view.product,)):
            for problem in json.loads(row['problems']):
                self.fixes.setdefault((problem['file'], problem['scenario'], problem.get('fingerprint')), []).append(dict(row))
        self.works, self.skipped = {}, {}
        for row in db.execute("SELECT id, machine, checkout, branch, seq, local_id, launch, operator, ended_at, result FROM work "
                              "WHERE product=? AND kind='run' ORDER BY seq", (view.product,)):
            row = dict(row)
            summary = json.loads(row.pop('result') or '{}')
            row['submitted_by'] = summary.get('submitted_by') or row['operator']
            self.works[row['id']] = row
            for item in summary.get('not_executed') or []:
                file, _, scenario = str(item.get('scenario', '')).partition('#')
                self.skipped.setdefault((file, scenario, item.get('fingerprint')), []).append(dict(
                    run=row['local_id'], record=row['id'], machine=row['machine'], checkout=row['checkout'], seq=row['seq'] or 0,
                    by=row['submitted_by'], at=row['ended_at'], finding=item.get('finding')))

    # ---- 选择 ----

    def own(self, row):
        return row['machine'] == self.viewer.machine and row['checkout'] == self.viewer.checkout

    def select(self, key):
        group = self.groups.get(key, [])
        fixes = self.fixes.get(key, [])
        conclusions = [row for row in group if conclusion(row)]
        branch = self.viewer.branch
        if branch and (any(r['branch'] == branch for r in conclusions) or any(f['branch'] == branch for f in fixes)):
            chosen = [r for r in conclusions if r['branch'] == branch]
            fixes = [f for f in fixes if f['branch'] == branch]
            fallback = False
        else:
            chosen, fallback = conclusions, bool(conclusions or fixes)
        records = sorted(chosen + [r for r in group if not conclusion(r) and self.own(r)], key=lambda r: r['seq'], reverse=True)
        return Selection(records, max((f['seq'] or 0 for f in fixes), default=None) or None, fallback)

    def own_guard(self, key):
        return next((run for run in self.guards.get(key, []) if self.own(run)), None)

    def own_guard_runs(self, key, limit=10):
        return [run for run in self.guards.get(key, []) if self.own(run)][:limit]

    def not_executed(self, scene):
        """本检出在当前指纹上最近一次未执行；没有记指纹的早先条目作为后备。"""
        for key in ((scene['file'], scene['id'], scene['fingerprint']), (scene['file'], scene['id'], None)):
            mine = [item for item in self.skipped.get(key, []) if self.own(item)]
            if mine:
                return max(mine, key=lambda item: item['seq'])
        return None

    def machine_name(self, machine):
        return self.names.get(machine, machine)

    def elsewhere(self, scene):
        """其他机器（及本机其他检出）上的情况：各自最近一次的受阻、跳过、未执行与守护未通过（已被那里更晚的结果取代的不列）。"""
        key = (scene['file'], scene['id'], scene['fingerprint'])
        places = {}

        def place(row):
            return places.setdefault((row['machine'], row['checkout']), {})
        for row in self.groups.get(key, []):
            if not self.own(row):
                slot = place(row)
                if 'result' not in slot:
                    slot['result'] = row
        for run in self.guards.get(key, []):
            if not self.own(run):
                slot = place(run)
                if 'guard' not in slot:
                    slot['guard'] = run
        for item in self.skipped.get(key, []):
            if not self.own(item):
                slot = place(item)
                if 'skipped' not in slot or item['seq'] > slot['skipped']['seq']:
                    slot['skipped'] = item
        items = []
        for (machine, checkout), slot in places.items():
            result, base = slot.get('result'), slot['result']['seq'] if slot.get('result') else 0
            common = dict(machine=self.machine_name(machine), checkout=checkout)
            if result and result['kind'] == 'agent' and result['status'] in ('blocked', 'skip'):
                work = self.works.get(result['work']) or {}
                items.append(dict(common, kind=result['status'], label=ELSEWHERE[result['status']], by=work.get('submitted_by'),
                                  at=result['executed_at'], reason=result['finding'], branch=result['branch'], seq=result['seq']))
            guard = slot.get('guard')
            if guard and guard['status'] == 'fail' and guard['seq'] > base:
                items.append(dict(common, kind='guard-failed', label=ELSEWHERE['guard-failed'], by=guard['operator'],
                                  at=guard['ran_at'], reason=guard['reason'], branch=guard['branch'], seq=guard['seq']))
            skipped = slot.get('skipped')
            if skipped and skipped['seq'] > base:
                items.append(dict(common, kind='not-executed', label=ELSEWHERE['not-executed'], by=skipped['by'],
                                  at=skipped['at'], reason=skipped['finding'], branch=None, seq=skipped['seq']))
        return sorted(items, key=lambda item: item['seq'], reverse=True)

    def involved(self, key, current):
        """一组（文件 + 场景 + 指纹）里参与本看板推导的记录，新到旧。

        current 为真（场景的当前内容）时按“场景状态”的规则选出（本分支优先，没有时回落到全部分支）；
        旧内容上只取本分支的结论和本检出的非结论，别的分支上内容不同的结果不列入，也不回落。"""
        if current:
            return self.select(key).records
        branch = self.viewer.branch
        return [row for row in self.groups.get(key, [])
                if (conclusion(row) and branch is not None and row['branch'] == branch) or (not conclusion(row) and self.own(row))]

    def history(self, scene):
        """场景的历史：各指纹上参与本看板推导的记录（见 involved），新到旧。"""
        rows = []
        for (file, scenario, fingerprint) in self.groups:
            if file == scene['file'] and scenario == scene['id']:
                rows += self.involved((file, scenario, fingerprint), fingerprint == scene['fingerprint'])
        return sorted(rows, key=lambda r: r['seq'], reverse=True)

    def participating(self):
        """全部参与本看板推导的结果记录（统计与人工确认率用，与历史同一规则），按序号旧到新。

        self.current（文件 + 场景 → 当前指纹）由 board() 填入；场景已不在本检出的，它的记录都按旧内容的规则取。"""
        rows = []
        for key in self.groups:
            rows += self.involved(key, self.current is None or self.current.get(key[:2]) == key[2])
        return sorted(rows, key=lambda r: r['seq'])


def derive(scene, selection, *, guard_run=None):
    """单个场景的状态。selection.records 为当前指纹上选出的记录（新到旧）；guard_run 为本检出最近一次守护运行。"""
    if scene.get('status') == 'draft':
        return dict(state='draft', reason='草稿，确认后才进入执行')
    results = selection.records
    if not results:
        return dict(state='not-run', reason='当前内容还没有结果')
    latest = results[0]
    source = latest['branch'] if selection.fallback and conclusion(latest) else None
    info = derive_state(results, latest, selection.fixed_seq, guard_run)
    if selection.fallback and conclusion(latest):
        info['source_branch'] = branch_label(source)
    return info


def derive_state(results, latest, fixed, guard_run):
    if fixed:
        manual = [r for r in results if r['kind'] == 'manual']
        failed_before = any(r['status'] == 'fail' and r['seq'] < fixed for r in manual)
        passed_after = any(r['status'] == 'pass' and r['seq'] > fixed for r in manual)
        if failed_before and not passed_after and not (latest['kind'] == 'manual' and latest['seq'] > fixed):
            return dict(state='retest', reason='修复已关闭，等待测试同学复测', cause='fix-closed', result=latest['id'])
    if latest['status'] == 'changed':
        return dict(state='changed', reason=manual_reason(latest) + '；按新需求修改场景文件后回到未执行', result=latest['id'])
    if latest['status'] in ('fail', 'blocked'):
        if fixed and fixed > latest['seq']:
            return dict(state='retest', reason='修复已关闭，等待复测', cause='fix-closed', result=latest['id'])
        reason = manual_reason(latest) if latest['kind'] == 'manual' else latest.get('finding') or ''
        return dict(state=latest['status'], reason=reason, result=latest['id'])
    if latest['status'] == 'skip':
        return dict(state='skip', reason=latest.get('finding') or '', result=latest['id'])
    if guard_run and guard_run['status'] == 'fail' and guard_run['seq'] > latest['seq']:
        return dict(state='retest', reason='守护测试未通过：' + (guard_run['reason'] or ''), cause='guard-failed',
                    result=latest['id'], guard_run=guard_run['id'])
    return dict(state='pass', reason='', result=latest['id'])


def draft_reviews(db, product, scenes):
    """草稿场景的审阅状态：场景键 → {state, label, approvers, approvals}。

    approvals 是当前指纹上未撤销的同意（按登记先后），approvers 是其中的同意人（去重）。db 是业务记录库。
    """
    drafts = {key: scene for key, scene in scenes.items() if scene.get('status') == 'draft'}
    if not drafts:
        return {}
    annotated = {(row['file'], row['target']) for row in db.execute(
        "SELECT file, target FROM annotations WHERE product=? AND status='open'", (product,))}
    active = {}
    for row in db.execute('SELECT id, file, scenario, fingerprint, approved_by, approved_at FROM draft_approvals '
                          'WHERE product=? AND revoked_at IS NULL ORDER BY id', (product,)):
        active.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
    result = {}
    for key, scene in drafts.items():
        approvals = active.get((scene['file'], scene['id'], scene['fingerprint']), [])
        state = 'annotated' if (scene['file'], scene['id']) in annotated else 'approved' if approvals else 'pending'
        result[key] = dict(state=state, label=DRAFT_REVIEW[state], approvals=approvals,
                           approvers=list(dict.fromkeys(a['approved_by'] for a in approvals)))
    return result


def board(book, scenes, *, guard_validity=None, reviews=None):
    """全部场景的状态与计数。guard_validity：场景键 → 守护判定（guards.validity）。"""
    reviews = reviews or {}
    guard_validity = guard_validity or {}
    book.current = {(scene['file'], scene['id']): scene['fingerprint'] for scene in scenes.values()}
    items, counts = {}, {state: 0 for state in ORDER}
    draft_counts = {state: 0 for state in DRAFT_REVIEW}
    distill = review = 0
    for scene_key, scene in scenes.items():
        identity = (scene['file'], scene['id'], scene['fingerprint'])
        selection = book.select(identity)
        results = selection.records
        guard = guard_validity.get(scene_key) or dict(valid=False, reason='没有守护')
        run = book.own_guard(identity)
        info = derive(scene, selection, guard_run=run)
        info['label'] = LABELS[info['state']]
        info['distill'] = bool(info['state'] == 'pass' and independent_pass(results) and not guard['valid']
                               and not guard.get('unsynced') and not guard.get('unrunnable'))
        info['review'] = info['state'] == 'pass' and accepted(results[0])
        info['guard'] = dict(guard, last=dict(id=run['id'], status=run['status'], reason=run['reason'], ran_at=run['ran_at'],
                                              product_digest=run['product_digest']) if run else None)
        if scene_key in reviews:
            info['draft_review'] = reviews[scene_key]
            draft_counts[reviews[scene_key]['state']] += 1
        counts[info['state']] += 1
        distill += info['distill']
        review += info['review']
        items[scene_key] = info
    return dict(items=items, counts=counts, distill=distill, review=review, total=len(scenes), draft_review=draft_counts,
                book=book)


def confirmation(rows):
    """Agent 独立通过的人工复核：样本 → of_result 指向它的最近一条人工通过或不通过，不比较快照。

    只有阻塞或需求变更的样本记为未决（取最近一条），不进分母。rows 按序号旧到新。
    """
    samples = {row['id']: row for row in rows if accepted(row)}
    verdicts, undecided = {}, {}
    for row in rows:
        sample = samples.get(row['of_result']) if row['kind'] == 'manual' else None
        if sample is None:
            continue
        (verdicts if row['status'] in ('pass', 'fail') else undecided)[sample['id']] = row
    undecided = {k: v for k, v in undecided.items() if k not in verdicts}
    misjudgments = [dict(scenario=f"{samples[k]['file']}#{samples[k]['scenario']}", fingerprint=samples[k]['fingerprint'],
                         result=k, run=samples[k]['work'], executor=samples[k]['executor'], agent_at=samples[k]['executed_at'],
                         machine=samples[k]['machine'], branch=samples[k]['branch'],
                         mark=m['id'], marked_at=m['executed_at'], marked_by=m['marked_by'], defect=m['defect'], note=m['note'])
                    for k, m in sorted(verdicts.items(), key=lambda item: item[1]['seq']) if m['status'] == 'fail']
    counts = {}
    for row in undecided.values():
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return dict(samples=len(samples), reviewed=len(verdicts),
                confirmed=sum(m['status'] == 'pass' for m in verdicts.values()),
                undecided=counts, misjudgments=misjudgments)


def statistics(book):
    """Agent 通过率、一次通过率、人工复测覆盖、人工确认率与独立性计数，只用参与本看板推导的记录。

    前四项按各指纹最新结果；人工确认率按全部 Agent 独立通过样本（见 confirmation）。
    """
    rows = book.participating()
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
