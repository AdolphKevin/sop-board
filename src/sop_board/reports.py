"""将同一读取快照中的批次统计和用例呈现为稳定的 Markdown。"""

from html import escape

from .regressions import RUN_STATUS, ci_label, coverage_lines
from .time_format import local_date
from .statistics import ORIGIN_LABELS, INDEPENDENCE_LABELS


STATUS = {"pass": "通过", "fail": "失败", "blocked": "受阻", "skip": "跳过"}
MANUAL = {**STATUS, "fail": "不通过（失败）", "blocked": "不通过（受阻）"}
FIX_METRICS = [('tasks', '任务数'), ('problems', '问题数'), ('merged', '已合入数'), ('session_closed', '会话修复已关闭数'),
               ('developer', '转开发者数'), ('first_pass', '首次尝试即通过的任务数'), ('attempts', '总尝试次数')]


def fraction(value):
    return f"{value['numerator']}/{value['denominator']}" if value["denominator"] else "无数据"


def cell(value):
    return escape(str(value), quote=False).replace("|", "&#124;").replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def table(headers, rows):
    if not rows:
        return "无\n"
    return ("| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" +
            "".join("| " + " | ".join(cell(v) for v in row) + " |\n" for row in rows))


def markdown(data, generated_at):
    version = data["version"]
    summary = data["summary"]
    scenarios = {item["scenario"]: item for item in summary["scenarios"]}
    workspace = data["workspace"]
    sections = ["# 验收报告\n\n## 头部\n\n" + "\n".join("- " + label + "：" + cell(value) for label, value in [
        ("工作区", workspace["name"]), ("仓库", "、".join(r["alias"] + "：" + r["identity"] for r in workspace["repositories"])), ("批次", data["batch"]["name"]),
        ("场景版本", version["version"] if version else "无"), ("推送提交号", version["commit_id"] if version else "无"),
        ("用例并集的最新上传序号", data["latest_upload"] or "无"),
        ("批次产品指纹", data["batch"]["product_digest"] or "无"), ("沿用场景", summary["reused_count"]), ("生成时间", generated_at)])]
    matched, unmatched = [], []
    pairings = {p["case"]: p for p in data["pairings"]}
    holds = data.get('holds', {})
    held = {h['case']: h for h in holds.get('held', [])}
    for case in data["cases"]:
        excluded = data.get('exclusions', {}).get(case['id'])
        if excluded:
            unmatched.append([case['id'], case['title'], f"本批不纳入：{excluded['text']}（{excluded['author']}，{local_date(excluded['created_at'])}）"])
            continue
        pair = pairings.get(case["id"])
        item = scenarios.get(pair["scenarios"][0]) if pair else None
        if item is None:
            unmatched.append([case["id"], case["title"], '；'.join(held.get(case['id'], {}).get('reasons', [])) or '等待合并'])
        else:
            a, m = item["latest_agent"], item["latest_manual"]
            matched.append([case["id"], case["title"], "细节并入" if pair["kind"] == "detail" else "流程", item["scenario"], pair["kind_reason"], STATUS[a["status"]] if a else "尚无 Agent 结果",
                            MANUAL[m["status"]] if m else "尚无人工结果",
                            '用例已更新，新内容待补充；' + '；'.join(held[case['id']]['reasons']) if case['id'] in held else ''])
    sections.append("## 用例覆盖\n\n用例覆盖：" + fraction(data["coverage"]) + "\n\n" +
                    table(["用例", "标题", "类别", "对应场景", "分类理由", "Agent 结果", "人工结果", "待补充说明"], matched) +
                    "\n### 尚无对应场景\n\n" + table(["用例", "标题", "暂缓原因或当前状态"], unmatched))
    if holds.get('gaps') or held:
        sections.append(f"## 待补充\n\n待补充 {len(holds['gaps'])} 项 · 暂缓 {len(held)} 条用例。暂缓的新内容不算作已验证；整条暂缓的场景不计入当前执行进度与通过率，保留旧细节的更新流程照常计入。历史结果保留。\n\n" +
                        table(['用例', '原因', '原有归属', '保留内容'], [[h['case'], '；'.join(h['reasons']), '、'.join(h.get('scenarios', [])) or '无',
                            '上一版细节预期与配对' if h.get('retention') == 'detail' else '整条旧场景与历史' if h.get('scenarios') else '无旧内容'] for h in held.values()]))
    stats = summary["statistics"]
    sections.append("## Agent 统计\n\n当前执行进度：" + fraction(stats['executed_progress']) + "\n\nAgent 验收通过率：" + fraction(stats["agent_pass_rate"]) +
                    "\n\n一次通过率：" + fraction(stats["first_pass_rate"]) + "\n\n" +
                    table(["轮次", "种类", "每轮通过率", "沿用场景数", "验证技能摘要"], [[f"第 {r['number']} 轮", "Agent" if r["kind"] == "agent" else "人工",
                                                               fraction(r["pass_rate"]), r["reused_count"], (r["skill_digest"] or "")[:12] or "无记录"] for r in summary["rounds"]]))
    sections.append("## 执行入口与独立性\n\n当前 Agent 结果（分别计数）：\n\n" +
        table(['类别', '场景数'], [[INDEPENDENCE_LABELS[k], v] for k, v in stats['independence_counts'].items()]) +
        "\n每轮身份记录：\n\n" + table(['轮次', '执行入口', '执行者', '实现者', '独立性'],
        [[r['number'], ORIGIN_LABELS[r['execution_origin']], '、'.join(r['executors']), r['implementer'] or '未声明',
          INDEPENDENCE_LABELS[r['independence']]] for r in summary['rounds']]))
    sections.append("## 沿用明细\n\n按执行轮次列出沿用场景及理由。\n\n" + table(["轮次", "场景", "沿用理由"],
        [[r["number"], item["scenario"], item["reason"]] for r in summary["rounds"] for item in r["reused_scenarios"]]))
    sections.append("## 人工复测\n\n人工复测通过率：" + fraction(stats["manual_pass_rate"]) +
                    "\n\n复测覆盖率：" + fraction(stats["retest_coverage"]) + "\n\n### 误判明细\n\n" +
                    table(["场景", "缺陷链接", "Agent 执行者", "标记人"], [[m["scenario"], m["defect_url"], m["agent_executor"], m["marker"]]
                          for m in summary["misjudgments"]]))
    sections.append("## 当前人工缺陷\n\n" + table(["场景", "缺陷链接"], [
        [item["scenario"], item["latest_manual"]["finding"]] for item in summary["scenarios"]
        if item["latest_manual"] and item["latest_manual"].get("finding")]))
    fix_statistics = data.get('fix_statistics')
    records = []
    for task in data.get('fixes', []):
        jira = '；'.join(f"{r['issue']}：{r['comment']} · {r['transition']} · {r['reason']}" for r in task['jira']) or '无 Jira 回写记录'
        records.append(f"### 修复任务 #{task['id']}\n\n" + '\n'.join('- ' + label + '：' + cell(value) for label, value in [
            ('问题场景', '、'.join(task['scenarios'])), ('问题数', task['problems']), ('根因摘要', task['root_cause'] or '尚未分析'),
            ('尝试次数', task['attempt']), ('结果', task['status']), ('原因', task['error']), ('Jira 回写结果', jira)]) + '\n\n' +
            table(['场景', '根因仓库'], [[r['batch'] + '/' + r['scenario'], r['root_repo'] or '未定位'] for r in task['root_repositories']]) + '\n' +
            table(['仓库', '身份', '改动文件', '修复提交', '基线分支', '合入提交', '状态', '原因'],
                [[r['alias'], r['identity'], '、'.join(r['changed_files']) or '无', r['repair_commit'], r['baseline_branch'],
                  r['merged_commit'] or '未合入', r['merge_status'], r['merge_reason']] for r in task['repositories']]))
    sections.append('## 修复记录\n\n' + ('\n\n'.join(records) + '\n\n### 自动修复统计\n\n' + '\n'.join(
        '- ' + label + '：' + str(fix_statistics[key]) for key, label in FIX_METRICS) if records else '暂无记录'))
    regressions = []
    for link in data.get('regression_links', []):
        covered, remaining = coverage_lines(link)
        detail = ''
        if 'command' in link:
            detail = '\n\n' + '\n'.join('- ' + label + '：' + cell(value) for label, value in [
                ('仓库路径', link['root']), ('测试文件', link['path']), ('测试名', link['test_name']),
                ('运行命令', link['command']), ('本机原始输出', link['local_run']['output']),
                ('CI 任务与命令', link['ci']['job'] + ' · ' + link['ci']['command']),
                ('CI 运行与提交', link['ci']['run'] + ' · ' + link['ci']['commit']), ('CI 原始输出', link['ci']['output'])])
        regressions.append('### ' + cell(link['scenario'] + ' · ' + link['test_id']) + '\n\n' +
            table(['状态', '仓库', '提交', '本机运行', 'CI', '独立验收'], [[link['status'], link['repo'], link['commit_id'] or '尚未提交',
                RUN_STATUS[link['local_run']['status']], ci_label(link['ci']),
                '已完成' if link['independent_acceptance'] == 'completed' else '独立验收未完成']]) + '\n' +
            table(['覆盖的预期', '仍需真实入口验收的部分'], [['；'.join(covered) or '无', '；'.join(remaining) or '无未覆盖项；独立验收仍须执行']]) + detail)
    sections.append('## 回归覆盖\n\n' + ('\n\n'.join(regressions) if regressions else '暂无记录'))
    merge = data.get('merge')
    if not merge:
        sections.append('## 合并记录\n\n暂无记录')
    else:
        removed = [[s['id'], '重复' if s['action'] == 'duplicate' else '冲突', s['target'], s['reason']]
                   for s in merge.get('scenarios', []) if s['action'] != 'kept']
        inferred = [[i['id'], i['content']] for i in merge.get('inferences', [])]
        sections.append(f"## 合并记录\n\n最近完成的合并任务：#{merge['id']}\n\n" +
                        table(['移除场景', '去向', '目标', '理由'], removed) +
                        '\n### 待确认的推断\n\n' + table(['编号', '内容'], inferred))
    unverified = []
    for item in summary["scenarios"]:
        latest = max((r for r in [item["latest_agent"], item["latest_manual"]] if r), key=lambda r: r["number"], default=None)
        if latest and latest["status"] in ("blocked", "skip"):
            unverified.append([item["scenario"], STATUS[latest["status"]], latest.get("finding") or latest.get("note") or "无"])
    sections.append("## 未验证范围\n\n" + table(["场景", "结果", "缺陷链接或原因"], unverified))
    return "\n\n".join(s.rstrip() for s in sections) + "\n"
