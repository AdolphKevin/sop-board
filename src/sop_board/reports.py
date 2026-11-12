"""检出的验收报告：Markdown 正文与所引证据的 zip。只含该检出的结果。"""
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


def build(context, *, file=None, local=True):
    """返回 (markdown, 证据列表[(zip 内路径, sha256)])。"""
    from .cli import scene_board
    import os
    project, checkout = context.project, context.checkout
    found, snapshot, derived = scene_board(context)
    scenes = {k: v for k, v in found['scenarios'].items() if file is None or v['file'] == file}
    if file is not None and file not in found['files']:
        raise ValueError(f'场景文件不存在：{file}')
    evidence, lines = [], []
    with context.store.connection() as db:
        stats = status.statistics(db, checkout.id)
        rows = {}
        for row in db.execute('SELECT * FROM results WHERE checkout=? ORDER BY id DESC', (checkout.id,)):
            rows.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
        files_of = {}
        for result in [r for group in rows.values() for r in group]:
            files_of[result['id']] = [dict(item) for item in db.execute(
                'SELECT name, sha256 FROM result_evidence WHERE result=? ORDER BY ordinal', (result['id'],))]
        guards = status.guard_runs(db, checkout.id)
        skipped = status.not_executed(db, checkout.id)
        from .envs import stopped_during, stopped_labels
        stopped = stopped_during(db, [r['work'] for group in rows.values() for r in group])

    def skipped_text(scene):
        item = status.latest_not_executed(skipped, scene)
        if not item:
            return None
        return (f"最近一次未执行：操作者 {item['by'] or '未署名'}，{local_time(item['at'])}，运行 #{item['run']}"
                + (f"，{_cell(item['finding'])}" if item.get('finding') else '') + '（未执行不改变状态）')
    title = f"# 验收报告：{project.name} · 检出 {checkout.name}" + (f" · 场景文件 {file}" if file else '')
    lines += [title, '', f"- 导出时间：{local_time(now())}",
              f"- 产品快照：{snapshot['product_digest'][:16]}（{'含未提交改动' if snapshot['dirty'] else '与提交一致'}）",
              '- 提交：' + '，'.join(f"{alias} {info['commit'][:10] if info['commit'] else '无提交'}"
                                  for alias, info in snapshot['repos'].items()), '']
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
    for key in sorted(scenes):
        scene, info = scenes[key], derived['items'][key]
        history = rows.get((scene['file'], scene['id'], scene['fingerprint']), [])
        lines += [f"### {key} {scene['title']}", '',
                  f"- 状态：**{info['label']}**" + (f"（{info['reason']}）" if info['reason'] else ''),
                  f"- 入口 {scene['verify']} · {scene['priority']} · {'正向' if scene['polarity'] == 'positive' else '反向'}"]
        if skipped_text(scene):
            lines.append('- ' + skipped_text(scene))
        guard = guards.get((scene['file'], scene['id'], scene['fingerprint']))
        if guard:
            lines.append(f"- 最近一次守护运行：{'通过' if guard['status'] == 'pass' else '未通过'}，{local_time(guard['ran_at'])}，"
                         f"快照 {_version(guard['product_digest'])}，运行者 {guard['operator']}"
                         + (f"；{_cell(guard['reason'])}" if guard['reason'] else '') + "（守护测试，不是真实入口执行）")
        for result in history[:5]:
            if result['kind'] == 'manual':
                lines.append(f"- 人工复测 {local_time(result['executed_at'])}：{status.MANUAL[result['status']]}，"
                             f"复测人 {result['marked_by'] or '未署名'}" + (f"，缺陷 {result['defect']}" if result['defect'] else '')
                             + (f"，说明 {result['note']}" if result['note'] else ''))
                continue
            label = {'pass': '通过', 'fail': '失败', 'blocked': '受阻', 'skip': '跳过'}[result['status']]
            if result['reused_from']:
                label += '（沿用早先结果的证据，未实际执行）'
            executor = result['executor'] if local else 'Agent 会话'
            changed = json.loads(result['product_changed'] or '[]')
            down = stopped.get(result['work'], {}).get(key, [])
            lines.append(f"- 运行 #{result['work']} {local_time(result['executed_at'])}：{label}，"
                         f"执行者 {executor}，{status.INDEPENDENCE.get(result['independence'], '独立性未声明')}，"
                         f"快照 {_version(result['product_digest'])}"
                         + (f"（执行期间变化：{_cell('、'.join(changed[:5]))}）" if changed else '')
                         + (f"；{'；'.join(s['text'] for s in stopped_labels(project, down))}" if down else '')
                         + (f"；{_cell(result['finding'])}" if result['finding'] else ''))
            for item in files_of.get(result['id'], []):
                path = f"evidence/{item['sha256'][:12]}-{item['name']}"
                evidence.append((path, item['sha256']))
                lines.append(f"  - 证据 [{item['name']}]({path})（sha256 {item['sha256']}）")
        lines.append('')
    lines += ['## 误判明细（Agent 独立通过、人工复测不通过）', '']
    for item in stats['misjudgments'] or []:
        executor = f"，执行者 {item['executor']}" if local else ''
        lines.append(f"- {item['scenario']}：运行 #{item['run']} {local_time(item['agent_at'])} Agent 独立通过{executor}；"
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
    secrets = [os.environ[n] for n in names(project.entries) if os.environ.get(n)]
    markdown = redact('\n'.join(lines).encode(), secrets).decode()
    return markdown, list(dict.fromkeys(evidence))


def export(context, *, file=None, zip_evidence=False, out=None):
    """写出 report.md（及 evidence.zip）。缺省目录为数据目录 reports/<检出>-<时间>/，不写进产品仓库。"""
    from datetime import datetime
    from pathlib import Path
    markdown, evidence = build(context, file=file)
    directory = Path(out) if out else context.project.data / 'reports' / (
        f"{context.checkout.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / 'report.md'
    report.write_text(markdown, encoding='utf-8')
    result = dict(ok=True, checkout=context.checkout.name, report=str(report), evidence=len(evidence))
    if zip_evidence:
        # 证据按登记时的原文放入 zip，sha256 与报告引用一致；执行时已按提示词脱敏。
        archive_path = directory / 'evidence.zip'
        with ZipFile(archive_path, 'x', ZIP_DEFLATED) as archive:
            archive.writestr('report.md', markdown)
            for path, sha in evidence:
                archive.writestr(path, context.store.read_evidence(sha))
        result['zip'] = str(archive_path)
    return result
