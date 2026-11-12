"""检出的验收报告：Markdown 正文与所引证据的 zip。与看板使用同一套状态推导（结论按分支归属、本分支优先，加本检出的非结论）；
逐条写明结果的产出机器与分支，结论来自其他分支的标出来源。别人机器上的证据：产出机器的看板可达时取回、核对 sha256 后放入 zip，
不可达时逐条写“证据在 <机器显示名> 的机器上，未包含”。报告不含共享库的主机、端口与库名。"""
import json
from zipfile import ZIP_DEFLATED, ZipFile

from . import status
from .redaction import names, redact
from .store import now
from .time_format import local_time


def _cell(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ')


def _ratio(item):
    if not item['denominator']:
        return '—'
    return f"{item['numerator']}/{item['denominator']}（{item['value']:.0%}）"


def _version(digest):
    return digest[:16] if digest else '代码版本未记录'


def _undecided(counts):
    return f"；未决：阻塞 {counts.get('blocked', 0)}，需求变更 {counts.get('changed', 0)}"


def build(context, *, file=None, local=True, fetch=False):
    """返回 (markdown, 证据列表[(zip 内路径, sha256, 产出机器)], 缺少的证据[(zip 内路径, sha256, 机器显示名)], 取回的证据{sha: bytes})。

    fetch=True 时向产出机器的看板取回本机没有的证据（report --zip 用）；看板页面只列出位置。"""
    from .cli import scene_board
    from . import peers
    import os
    project, checkout, store = context.project, context.checkout, context.store
    found, snapshot, derived = scene_board(context)
    book = derived['book']
    scenes = {k: v for k, v in found['scenarios'].items() if file is None or v['file'] == file}
    if file is not None and file not in found['files']:
        raise ValueError(f'场景文件不存在：{file}')
    evidence, missing, fetched, lines = [], [], {}, []
    stats = status.statistics(book)
    from .envs import stopped_during, stopped_labels
    with store.records() as db:
        files_of = {}
        histories = {key: [r for r in book.select((s['file'], s['id'], s['fingerprint'])).records] for key, s in scenes.items()}
        for rows in histories.values():
            for result in rows:
                files_of[result['id']] = [dict(item) for item in db.execute(
                    'SELECT name, sha256 FROM result_evidence WHERE result=? ORDER BY ordinal', (result['id'],))]
        stopped = stopped_during(db, [r['work'] for rows in histories.values() for r in rows])
        available = set()
        for rows in histories.values():
            for result in rows[:5]:
                for item in files_of.get(result['id'], []):
                    sha = item['sha256']
                    if store.has_evidence(sha) or sha in available:
                        available.add(sha)
                        continue
                    others = [m for m in peers.producers(db, store.product, sha) if m != store.machine['id']]
                    if fetch:
                        raw = peers.fetch(db, sha, others)
                        if raw is not None:
                            fetched[sha] = raw
                            available.add(sha)
                    elif any(peers.reachable(db, m) for m in others):
                        available.add(sha)

    def skipped_text(scene):
        item = book.not_executed(scene)
        if not item:
            return None
        return (f"最近一次未执行：操作者 {item['by'] or '未署名'}，{local_time(item['at'])}，运行 #{item['run']}"
                + (f"，{_cell(item['finding'])}" if item.get('finding') else '') + '（未执行不改变状态）')
    branch = book.viewer.branch
    title = f"# 验收报告：{project.name} · 检出 {checkout.name}" + (f" · 场景文件 {file}" if file else '')
    lines += [title, '', f"- 导出时间：{local_time(now())}",
              f"- 产品：{store.product_name()}" + ('（共享库）' if store.shared else '（本机）')
              + f"；导出机器 {store.machine['name']}；分支 {status.branch_label(branch)}",
              f"- 产品快照：{snapshot['product_digest'][:16]}（{'含未提交改动' if snapshot['dirty'] else '与提交一致'}）",
              '- 提交：' + '，'.join(f"{alias} {info['commit'][:10] if info['commit'] else '无提交'}"
                                  for alias, info in snapshot['repos'].items()),
              '- 状态推导：本分支上有结论的场景只用本分支的结论；没有的用全部分支的结论并标出来源分支；受阻、跳过与守护未通过只取本检出的。',
              '__EVIDENCE_LINE__', '']
    counts = {}
    for key in scenes:
        state = derived['items'][key]['state']
        counts[state] = counts.get(state, 0) + 1
    lines += ['## 概况', '', '| 状态 | 场景数 |', '| --- | --- |']
    lines += [f"| {status.LABELS[state]} | {counts[state]} |" for state in status.ORDER if counts.get(state)]
    lines += ['', f"- Agent 通过率：{_ratio(stats['agent_pass_rate'])}；一次通过率：{_ratio(stats['first_pass_rate'])}",
              f"- 人工复测覆盖：{_ratio(stats['manual_coverage'])}；人工复测通过率：{_ratio(stats['manual_pass_rate'])}",
              f"- Agent 独立通过的人工确认率：{_ratio(stats['agent_confirm_rate'])}；复核覆盖：{_ratio(stats['agent_review_coverage'])}"
              + _undecided(stats['review_undecided']) + "（人工复核过的经真实入口 Agent 独立通过）",
              '- 独立性（各场景最新 Agent 结果）：' + ('，'.join(f"{status.INDEPENDENCE.get(k, k)} {v}"
                                                       for k, v in sorted(stats['independence'].items())) or '—'), '']
    lines += ['## 场景', '']

    def origin(result):
        text = f"产出机器 {book.machine_name(result['machine'])}，分支 {status.branch_label(result['branch'])}"
        if branch != result['branch'] and status.conclusion(result):
            text += f"（来源分支 {status.branch_label(result['branch'])}）"
        return text
    for key in sorted(scenes):
        scene, info = scenes[key], derived['items'][key]
        history = histories[key]
        lines += [f"### {key} {scene['title']}", '',
                  f"- 状态：**{info['label']}**" + (f"（{info['reason']}）" if info['reason'] else '')
                  + (f"；结论来自其他分支，来源分支 {info['source_branch']}" if info.get('source_branch') else ''),
                  f"- 入口 {scene['verify']} · {scene['priority']} · {'正向' if scene['polarity'] == 'positive' else '反向'}"]
        if skipped_text(scene):
            lines.append('- ' + skipped_text(scene))
        if info['guard'].get('unsynced') or info['guard'].get('unrunnable'):
            lines.append(f"- 守护：{info['guard']['reason']}")
        guard = book.own_guard((scene['file'], scene['id'], scene['fingerprint']))
        if guard:
            lines.append(f"- 最近一次守护运行：{'通过' if guard['status'] == 'pass' else '未通过'}，{local_time(guard['ran_at'])}，"
                         f"快照 {_version(guard['product_digest'])}，运行者 {guard['operator']}"
                         + (f"；{_cell(guard['reason'])}" if guard['reason'] else '') + "（守护测试，不是真实入口执行）")
        for item in book.elsewhere(scene):
            lines.append(f"- 其他机器上的情况：{item['machine']}（检出 {item['checkout']}）{item['label']}，{item['by'] or '未署名'}，"
                         f"{local_time(item['at']) if item['at'] else '时间未记录'}" + (f"，{_cell(item['reason'])}" if item['reason'] else '')
                         + '（不改变本检出的状态）')
        for result in history[:5]:
            if result['kind'] == 'manual':
                lines.append(f"- 人工复测 {local_time(result['executed_at'])}：{status.MANUAL[result['status']]}，"
                             f"复测人 {result['marked_by'] or '未署名'}" + (f"，缺陷 {result['defect']}" if result['defect'] else '')
                             + (f"，说明 {result['note']}" if result['note'] else '') + f"；{origin(result)}")
                continue
            label = {'pass': '通过', 'fail': '失败', 'blocked': '受阻', 'skip': '跳过'}[result['status']]
            if result['reused_from']:
                label += '（沿用早先结果的证据，未实际执行）'
            executor = result['executor'] if local else 'Agent 会话'
            changed = json.loads(result['product_changed'] or '[]')
            down = stopped.get(result['work'], {}).get(key, [])
            work = book.works.get(result['work']) or {}
            implementer = f"，实现者 {result['implemented_by']}（操作者）" if result['implemented_by'] else ''
            lines.append(f"- 运行 #{work.get('local_id', '?')} {local_time(result['executed_at'])}：{label}，"
                         f"执行者 {executor}{implementer}，{status.INDEPENDENCE.get(result['independence'], '独立性未声明')}，"
                         f"{origin(result)}，快照 {_version(result['product_digest'])}"
                         + (f"（执行期间变化：{_cell('、'.join(changed[:5]))}）" if changed else '')
                         + (f"；{'；'.join(s['text'] for s in stopped_labels(project, down))}" if down else '')
                         + (f"；{_cell(result['finding'])}" if result['finding'] else ''))
            for item in files_of.get(result['id'], []):
                path = f"evidence/{item['sha256'][:12]}-{item['name']}"
                if item['sha256'] in available:
                    evidence.append((path, item['sha256']))
                    lines.append(f"  - 证据 [{item['name']}]({path})（sha256 {item['sha256']}）")
                else:
                    where = book.machine_name(result['machine'])
                    missing.append((path, item['sha256'], where))
                    lines.append(f"  - 证据 {item['name']}（sha256 {item['sha256']}）：证据在 {where} 的机器上，未包含")
        lines.append('')
    lines += ['## 误判明细（Agent 独立通过、人工复测不通过）', '']
    for item in stats['misjudgments'] or []:
        executor = f"，执行者 {item['executor']}" if local else ''
        run = (book.works.get(item['run']) or {}).get('local_id', '?')
        lines.append(f"- {item['scenario']}：运行 #{run} {local_time(item['agent_at'])} Agent 独立通过{executor}"
                     f"（{book.machine_name(item['machine'])}，分支 {status.branch_label(item['branch'])}）；"
                     f"{local_time(item['marked_at'])} 人工复测不通过，复测人 {item['marked_by'] or '未署名'}"
                     + (f"，缺陷 {_cell(item['defect'])}" if item['defect'] else '')
                     + (f"，说明 {_cell(item['note'])}" if item['note'] else ''))
    if not stats['misjudgments']:
        lines.append('- 无')
    review = sorted(k for k in scenes if derived['items'][k]['review'])
    lines += ['', '## 待人工复核', '', *([f"- {k} {scenes[k]['title']}" for k in review] or ['- 无']), '']
    unexecuted = sorted(k for k in scenes if derived['items'][k]['state'] in ('not-run', 'retest', 'draft'))
    lines += ['## 未执行范围', '', *([f"- {k}：{derived['items'][k]['label']}"
                                     + (f"；{skipped_text(scenes[k])}" if skipped_text(scenes[k]) else '')
                                     for k in unexecuted] or ['- 无']), '']
    missing_count = len(dict.fromkeys(missing))
    lines[lines.index('__EVIDENCE_LINE__')] = (f"- 证据：本报告引用 {len(dict.fromkeys(evidence)) + missing_count} 份，其中 {missing_count} 份在别的机器上、"
                                              '产出机器的看板此刻读不到（逐条写明在哪台机器上）' if missing_count else
                                              f"- 证据：本报告引用 {len(dict.fromkeys(evidence))} 份，都取得到")
    secrets = [os.environ[n] for n in names(project.entries) if os.environ.get(n)]
    markdown = redact('\n'.join(lines).encode(), secrets).decode()
    return markdown, list(dict.fromkeys(evidence)), list(dict.fromkeys(missing)), fetched


def export(context, *, file=None, zip_evidence=False, out=None):
    """写出 report.md（及 evidence.zip）。缺省目录为数据目录 reports/<检出>-<时间>/，不写进产品仓库。"""
    from datetime import datetime
    from pathlib import Path
    markdown, evidence, missing, fetched = build(context, file=file, fetch=zip_evidence)
    directory = Path(out) if out else context.project.data / 'reports' / (
        f"{context.checkout.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / 'report.md'
    report.write_text(markdown, encoding='utf-8')
    result = dict(ok=True, checkout=context.checkout.name, report=str(report), evidence=len(evidence),
                  missing_evidence=len(missing),
                  missing=[dict(path=path, sha256=sha, where=f'证据在 {where} 的机器上') for path, sha, where in missing])
    result['evidence_note'] = ('没有缺少的证据' if not missing
                               else f'缺少 {len(missing)} 份证据（在别的机器上，取不到），报告里逐条写明“证据在 <机器> 的机器上，未包含”')
    if zip_evidence:
        # 证据按登记时的原文放入 zip，sha256 与报告引用一致；从别的机器取回的已核对 sha256。执行时已按提示词脱敏。
        archive_path = directory / 'evidence.zip'
        with ZipFile(archive_path, 'x', ZIP_DEFLATED) as archive:
            archive.writestr('report.md', markdown)
            for path, sha in evidence:
                archive.writestr(path, fetched[sha] if sha in fetched else context.store.read_evidence(sha))
        result['zip'] = str(archive_path)
    return result
