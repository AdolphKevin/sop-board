"""把 Service 的读取结果呈现为服务端 HTML，不重新计算业务指标。"""

from html import escape
from difflib import SequenceMatcher
import json
import re
from urllib.parse import quote, urlsplit

from .jira import identify
from .time_format import local_time
from .sop_markdown import render as render_sop
from .drafts import generated_case


def text(value):
    return escape(str(value), quote=True)


def batch_url(workspace, batch):
    return f"/w/{quote(workspace, safe='')}/b/{quote(batch, safe='')}"


def time_tag(value, element_id=None):
    identifier = f" id='{text(element_id)}'" if element_id else ""
    return f"<time{identifier} datetime='{text(value)}'>{local_time(value)}</time>"


def defect_link(value):
    value = value or ''
    try:
        clickable = urlsplit(value).scheme in ('http', 'https')
    except ValueError:
        clickable = False
    content = text(value)
    if clickable:
        jira = " data-jira-link=''" if identify(value) else ''
        content = f"<a href='{text(value)}' rel='noopener noreferrer'{jira}>{content}</a>"
    return "<span class='defect-link'>" + content + "</span>"


def fix_waiting(review):
    return "<div data-fix-waiting=''>" + ''.join(
        "<p class='notice'>" + text(reason) + "</p>" for reason in review.get('waiting', [])) + "</div>"


def review_completion(workspace, name, data):
    review = data['review']
    count = review['manual_count']
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}/review-complete"
    return (f"<section data-review-complete='{text(api)}'>"
            f"<button type='button' data-review-complete-button=''{'' if count else ' disabled'}>"
            f"复核完成，开始修复（{count} 条人工不通过）</button>"
            f"<p data-review-complete-empty=''>{'没有待修复的人工不通过' if not count else ''}</p>"
            "<p data-review-complete-message='' role='status' aria-live='polite'></p>" +
            fix_waiting(review) + "</section>")


def layout(title, content, watch=None, scripts=()):
    polling = ""
    if watch:
        workspace, name, etag = watch
        api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}/state"
        polling = f" data-state-url='{text(api)}' data-etag='{text(etag)}'"
        content = "<p id='refresh-message' class='notice' role='status' aria-live='polite' hidden></p>" + content
    extra_scripts = ''.join(f"<script src='{text(src)}' defer></script>" for src in scripts)
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{text(title)} · Flow Board</title>"
            "<link rel='stylesheet' href='/assets/board.css'><script src='/assets/board.js' defer></script>"
            f"{extra_scripts}</head><body>"
            "<a class='skip-link' href='#board-main'>跳到主要内容</a>"
            "<header class='app-header'><div class='brand'><a href='/'>Flow Board</a>"
            "<span>流程验收看板</span></div><span class='header-caption'>验收工作台</span></header>"
            f"<main id='board-main'{polling}>{content}</main></body></html>")


def environment_panel(data, local):
    if data is None:
        return ''
    content = ("<section id='environment-panel' data-environment-url='/api/v1/environment'>"
               "<h2>环境</h2><p id='environment-state'>环境" + text(data['status']) + '</p>' +
               "<p id='environment-check-state'>" + ('最近自检通过' if data['check_passed'] else '尚未通过自检') +
               (' · ' + text(data['checked_at']) if data['checked_at'] else '') + '</p>' +
               "<p id='environment-ports'>环境端口：" + text('、'.join(map(str, data['ports'])) or '未声明') + '</p>')
    instructions = data['setup_instructions']
    hidden = '' if instructions else ' hidden'
    content += (f"<div id='environment-setup'{hidden}>" +
                ''.join('<p>' + text(line) + '</p>' for line in instructions) + '</div>')
    if local:
        content += ("<p id='environment-jira'>Jira 读取：" +
                    ('已配置' if data.get('jira_configured') else '未配置') + "</p>")
        content += ''.join('<p>' + text(p['field'] + '：' + p['problem']) + '</p>' for p in data.get('problems', []))
        content += ("<button type='button' data-environment-action='/api/v1/environment/check'>自检</button> "
                    "<button type='button' data-environment-action='/api/v1/environment/restart'>重启环境</button>"
                    "<p id='environment-message' role='status'></p><h3>最近 50 行环境日志</h3><pre id='environment-log'>" +
                    text('\n'.join(data.get('log_lines', []))) + '</pre>')
        record = data.get('latest_check')
        if record:
            content += "<ol id='environment-check-steps'>" + ''.join('<li>' + text(step['name'] + '：' + step['status'] + ' · ' + step['message']) + '</li>' for step in record['steps']) + '</ol>'
        if not record:
            content += "<ol id='environment-check-steps'></ol>"
        for alias, repo in data.get('state', {}).get('repositories', {}).items():
            content += '<p>' + text(alias + '：' + repo['directory'] + ' · ' + repo['commit']) + '</p>'
    return content + '</section>'


def sop_upload(workspace, hashes, batch=''):
    return (f"<form data-sop-upload='' data-workspace-key='{text(workspace)}' data-sop-hashes='{text(json.dumps(hashes))}'>"
            "<h3>上传业务 SOP</h3>"
            f"<label>批次名称<input name='batch' value='{text(batch)}'{' readonly' if batch else ''} required></label>"
            "<label>SOP 文件<input type='file' name='file' accept='.md,.txt' required></label>"
            "<label>业务确认人<input name='confirmed_by' required></label>"
            "<button type='submit'>上传业务 SOP</button><p data-sop-message='' role='status'></p>"
            "<ul data-sop-problems=''></ul></form>")


def home(data, *, environment=None, local=False):
    cards = []
    for workspace in data['workspaces']:
        batches = ''.join(f"<li><a href='{text(batch_url(workspace['key'], batch['name']))}'>{text(batch['name'])}</a> · {agent_state(batch)}" +
                          execution_controls(workspace['key'], batch['name'], batch, local=local) + '</li>' for batch in workspace['batches'])
        repositories = ''.join(f"<p>{text(repo['alias'])}：{text(repo['identity'])}</p>" for repo in workspace['repositories'])
        cards.append(f"<section><h2>{text(workspace['name'])}</h2>{repositories}<ul>{batches}</ul>" +
            ''.join(f"<p><a href='/w/{text(workspace['key'])}/fixes?task={t['id']}'>修复任务 #{t['id']}</a> · {text(t['status'])}</p>" for t in workspace['fix_tasks']) +
            f"<form id='cases-upload' data-workspace-key='{text(workspace['key'])}'>"
            "<h3>上传批次用例</h3><label>批次名称<input name='batch' required></label>"
            "<label>用例文件<input type='file' name='file' accept='.yaml,.yml' required></label>"
            "<button type='submit'>上传用例</button><p data-cases-message='' role='status'></p>"
            "<ul data-cases-problems=''></ul></form>" +
            sop_upload(workspace['key'], workspace['sop_hashes']) + '</section>')
    return layout('工作区与批次', environment_panel(environment, local) + automation_controls(data, local=local) +
                  '<h1>工作区与批次</h1>' + identity_bar() + "<div data-execution-home=''>" + ''.join(cards) + '</div>')


def ratio(value):
    return f"{value['numerator']}/{value['denominator']}" if value["denominator"] else "无数据"


def agent_state(data):
    state = execution_state(data)
    return state + (' · 已暂停' if data.get('automation_paused') else '')


def execution_state(data):
    running = [t for t in data.get('execution_tasks', []) if t['kind'] == 'execute' and t['status'] == '运行中']
    if running:
        return f"执行中（{len(running)} 个会话，开始于 {local_time(running[0]['started_at'])}，任务 #{running[0]['id']}）"
    waiting = [t for t in data.get('execution_tasks', []) if t['kind'] == 'execute' and t['status'] == '等待合并']
    if waiting:
        return f"任务 #{waiting[0]['id']}：{text(waiting[0]['error'])}"
    if any(t['kind'] == 'execute' and t['status'] == '待提交' for t in data.get('execution_tasks', [])):
        return '待提交：工作区有未提交的产品改动'
    if data["version"] is None:
        return "尚无场景"
    if data["queue_counts"]["pending"]:
        return f"有待执行 {data['queue_counts']['pending']} 条"
    execution = data["agent_execution"]
    counts = execution["counts"]
    submitted = time_tag(execution["submitted_at"]) if execution["submitted_at"] else "无"
    return (f"Agent 已测完 · 最近 Agent 轮次提交时间：{submitted} · "
            f"通过 {counts['pass']}、失败 {counts['fail']}、受阻 {counts['blocked']}、跳过 {counts['skip']}")


def automation_controls(data, *, local=False):
    paused = data.get('automation_paused', False)
    notice = "<p id='automation-state' role='status'>自动任务已暂停</p>" if paused else "<p id='automation-state' role='status' hidden></p>"
    if not local:
        return "<section>" + notice + "</section>"
    return ("<section>" + notice + f"<button type='button' data-automation-toggle='' data-developer-action='/api/v1/automation/{'resume' if paused else 'pause'}'>"
            f"{'恢复自动任务' if paused else '暂停自动任务'}</button>"
            "<p id='developer-message' role='status' aria-live='polite'></p></section>")


def execution_controls(workspace, name, data, *, local=False):
    if not local:
        return ''
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}"
    options = data['execution']
    content = f"<div class='execution-controls' data-execution-url='{text(api)}'>"
    for scope, label, count in [('pending', '执行待执行场景', options['pending_count']),
                                ('all', '重新验证全部场景', options['total_count'])]:
        reason = options[scope + '_reason']
        reason_id = 'execution-' + quote(workspace, safe='') + '-' + quote(name, safe='') + '-' + scope
        content += (f"<div class='execution-choice'><button type='button' data-developer-action='{text(api)}/executions' "
                    f"data-execution-scope='{scope}' aria-describedby='{text(reason_id)}'" +
                    (' disabled' if reason else '') + f">{label}（{count} 条）</button>"
                    f"<span id='{text(reason_id)}' data-execution-reason='{scope}'>{text(reason)}</span></div>")
    return content + "<p data-execution-message='' role='status' aria-live='polite'></p></div>"


def execution_list(data):
    if not data.get('execution_tasks'):
        return ''
    rows = ''.join(f"<tr><th>#{t['id']}</th><td>执行</td>"
        f"<td>{text(t['trigger'])}</td><td>{len(t['scope'])}</td><td>{text(t['status'])}</td>"
        f"<td>{time_tag(t['started_at']) if t['started_at'] else ''}</td>"
        f"<td>{time_tag(t['finished_at']) if t['finished_at'] else ''}</td>"
        f"<td>{text(t['void_reason'] or t['error'])}</td><td>{t['reused_count']}</td><td>{text('、'.join('第 '+str(n)+' 轮' for n in t['rounds']))}</td></tr>"
        for t in data['execution_tasks'])
    return ('<section><h2>执行任务</h2><div class="table-scroll"><table><thead><tr><th>编号</th><th>种类</th>'
        '<th>触发原因</th><th>范围条数</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>原因</th><th>沿用</th><th>轮次</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table></div></section>')


def batch(workspace, data, *, environment=None, local=False):
    base = batch_url(workspace, data["name"])
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(data['name'], safe='')}"
    workspace_info = "<p>" + ("工作区已搭建" if data.get("configured") else "尚未搭建") + "</p>"
    if data.get('entry_problems'):
        workspace_info += '<section><h2>入口声明不合格</h2>' + ''.join(
            f"<p>{text(p['problem'])}</p>" for p in data['entry_problems']) + '</section>'
    if data.get("workspace_problems"):
        workspace_info += "<section><h2>工作区场景文件不合格</h2>" + "".join(
            f"<p>{text(p.get('path', ''))} 第 {p['line']} 行：{text(p['field'])} {text(p['problem'])}</p>"
            for p in data["workspace_problems"]) + "</section>"
    report_link = environment_panel(environment, local) + workspace_info + execution_list(data) + f"<p><a href='{text(api + '/report.md')}'>导出报告</a></p>"
    report_link += execution_controls(workspace, data['name'], data, local=local)
    report_link += automation_controls(data, local=local) + identity_bar()
    report_link += sop_upload(workspace, {data['name']: data['sop_sha256']}, data['name'])
    report_link += review_completion(workspace, data['name'], data)
    if data.get('fix_statistics', {}).get('tasks'):
        from .reports import FIX_METRICS
        report_link += '<section><h2>自动修复统计</h2>' + ''.join(
            f"<p>{label}：{data['fix_statistics'][key]}</p>" for key, label in FIX_METRICS) + '</section>'
    if data["version"] is None:
        return layout("批次 " + data["name"], batch_nav(workspace, data["name"], "overview") +
                      f"<h1>批次 {text(data['name'])}</h1><p>尚无场景，测试岗上传用例后由合并生成</p>"
                      f"<p id='agent-state'>{agent_state(data)}</p>{report_link}"
                      f"<a href='{text(base + '/cases')}'>用例页</a>", watch=(workspace, data["name"], data["etag"]))
    def metrics_for(items):
        return "".join(f"<div id='metric-{key}'><dt>{label}</dt><dd>{ratio(data['statistics'][key])}</dd></div>"
                       for key, label in items)

    metrics = metrics_for([("executed_progress", "已执行进度"), ("agent_pass_rate", "Agent 验收通过率"),
                           ("reviewed_progress", "已复核进度")])
    more_metrics = metrics_for([("manual_pass_rate", "人工复测通过率"), ("retest_coverage", "复测覆盖率"),
                                ("first_pass_rate", "一次通过率")])
    scenario_names = data.get('scenario_titles', {})
    descriptions = {'pending': '尚未完成执行的场景', 'issues': '失败或受阻，需要跟进', 'retest': 'Agent 已通过，等待人工复核'}
    active_queue = next((key for key in ('issues', 'pending', 'retest') if data['queue_counts'][key]), 'retest')
    queues = []
    for key, label in [("pending", "待执行"), ("issues", "问题"), ("retest", "复测")]:
        links = "".join(f"<li><a data-scenario='{text(s)}' href='{text(base + '/s/' + quote(s, safe=''))}'>"
                        f"<strong class='queue-title'>{text(scenario_names.get(s, s))}</strong>"
                        f"<small class='queue-id'>{text(s)}</small></a></li>"
                        for s in data["queues"][key])
        emphasis = ' queue-active' if key == active_queue else ''
        queues.append(f"<section id='queue-{key}' class='queue-panel{emphasis}'><div class='section-heading'>"
                      f"<h2>{label} <span data-count='{key}'>{data['queue_counts'][key]}</span></h2>"
                      f"<p>{descriptions[key]}</p></div><ul class='queue-list'>{links}</ul>" +
                      ("" if links else "<p class='empty-state'>暂无场景</p>") + "</section>")
    rounds = "".join(f"<tr data-round='{r['number']}'><th scope='row'>第 {r['number']} 轮</th>"
                     f"<td>{'Agent' if r['kind'] == 'agent' else '人工'}</td><td>{r['version']}</td>"
                     f"<td>{text(r['submitted_by'])}</td><td>{time_tag(r['submitted_at'])}</td>"
                     f"<td data-field='pass-rate'>{ratio(r['pass_rate'])}</td>"
                     f"<td><a href='{text(api + '/rounds/' + str(r['number']) + '/export')}'>导出 ZIP</a></td></tr>" for r in data["rounds"])
    findings = "".join(f"<tr><td><a href='{text(base + '/s/' + quote(item['scenario'], safe=''))}'>{text(item['scenario'])}</a></td>"
                       f"<td>{defect_link(item['defect_url'])}</td>"
                       f"<td>{text(item['agent_executor'])}</td><td>{text(item['marker'])}</td></tr>"
                       for item in data["misjudgments"])
    access = '工作区已搭建' if data.get('configured') else '尚未搭建'
    rounds = rounds or '<tr><td colspan="7" class="empty-state">尚无执行轮次</td></tr>'
    findings = findings or '<tr><td colspan="4" class="empty-state">暂无误判记录</td></tr>'
    content = ("<nav class='breadcrumbs' aria-label='位置'><a href='/'>全部工作区</a><span>/</span><span>批次总览</span></nav>"
               f"<div class='page-heading'><div><h1>验收总览</h1><p class='batch-id'>{text(data['name'])}</p>"
               f"<p class='batch-meta'><span class='access-badge'>{access}</span> 场景版本 {data['version']} · 当前场景 {data['scenario_count']} 条 · 沿用 {data['reused_count']} 条</p></div>"
               f"<div class='page-actions'><a class='button button-primary' href='{text(base + '/scenarios')}'>查看场景</a>"
               f"<a class='button button-secondary' href='{text(api + '/report.md')}'>导出报告</a></div></div>"
               f"{batch_nav(workspace, data['name'], 'overview')}"
               f"<div class='execution-banner'><span class='status-label'>执行状态</span><p id='agent-state'>{agent_state(data)}</p></div>"
               f"<dl class='metrics primary-metrics'>{metrics}</dl>"
               "<details class='metric-details' data-disclosure='metrics'><summary>更多验收指标</summary>"
               f"<dl class='metrics'>{more_metrics}</dl></details>"
               f"<div class='workspace-heading'><h2>场景工作区</h2><span>{data['scenario_count']} 条场景</span></div>"
               f"<div class='queues'>{''.join(queues)}</div>"
               "<section class='rounds-panel'><h2>轮次记录</h2><div class='table-scroll' tabindex='0' role='region' aria-label='轮次记录表'><table><thead><tr><th>轮次</th><th>种类</th>"
               "<th>场景版本</th><th>提交人</th><th>提交时间</th><th>每轮通过率</th><th>导出</th></tr></thead>"
               f"<tbody>{rounds}</tbody></table></div></section>"
               f"<section id='misjudgments' class='misjudgments-panel'><h2>误判明细 <span id='misjudgment-count'>{len(data['misjudgments'])}</span></h2>"
               "<div class='table-scroll'><table><thead><tr><th>场景</th><th>缺陷链接</th><th>Agent 执行者</th><th>标记人</th></tr></thead>"
               f"<tbody>{findings}</tbody></table></div></section>"
               f"{report_link}<p class='page-footnote'>场景文件推送时间：{time_tag(data['pushed_at'], 'pushed-at')}</p>")
    return layout("批次 " + data["name"], content, watch=(workspace, data["name"], data["etag"]))


STATUS = {"pass": "通过", "fail": "失败", "blocked": "受阻", "skip": "跳过"}
MANUAL_STATUS = {**STATUS, "fail": "不通过（失败）", "blocked": "不通过（受阻）"}


def evidence_preview(evidence):
    url = "/api/v1/evidence/" + evidence["sha256"]
    download = f"<a href='{text(url)}' download='{text(evidence['name'])}'>下载 {text(evidence['name'])}</a>"
    if evidence["content_type"].startswith("video/"):
        return (f"<figure class='evidence-video'><video controls playsinline preload='metadata' "
                f"aria-label='录屏：{text(evidence['name'])}'>"
                f"<source src='{text(url)}' type='{text(evidence['content_type'])}'>"
                f"浏览器无法播放此录屏，请使用下载链接。</video>"
                f"<figcaption>录屏：{text(evidence['name'])}</figcaption></figure><p>{download}</p>")
    if evidence["content_type"].startswith("image/"):
        return download + f"<figure><img src='{text(url)}' alt='{text(evidence['name'])}' loading='lazy'><figcaption>{text(evidence['name'])}</figcaption></figure>"
    if "preview" in evidence:
        preview = evidence["preview"]
        caption = evidence["name"] + ("（只显示前 200 行）" if evidence.get("preview_truncated") else "")
        if re.fullmatch(r"observation-[0-9]+\.json", evidence["name"]):
            try:
                observation = json.loads(preview)
                time_field = "recorded_at" if "recorded_at" in observation else "executed_at"
                if isinstance(observation, dict) and isinstance(observation.get(time_field), str):
                    observation[time_field] = local_time(observation[time_field])
                    preview = json.dumps(observation, ensure_ascii=False, indent=2)
                    caption = evidence["name"] + "（观察记录，本地时间）"
            except (ValueError, TypeError):
                pass
        return download + f"<figure><figcaption>{text(caption)}</figcaption><pre data-evidence='{text(evidence['sha256'])}'>{text(preview)}</pre></figure>"
    return f"<p>{download}</p>"


def scenario(data):
    base = batch_url(data["workspace"], data["batch"])
    titles = {}
    for source in data.get('sop_sources', []):
        _, headings = render_sop(source['content'], source['path'])
        for heading in headings:
            titles.setdefault(heading['title'], base + '/sop#' + quote(heading['anchor'], safe=''))
    sections = []
    for group in data["groups"]:
        label = ("当前内容" if group["current"] else "移除前内容" if group["fingerprint"] == data["fingerprint"] else "场景已变更")
        fields = []
        for key, name in [("initial_state", "初始状态"), ("steps", "操作步骤"), ("expected", "预期结果"), ("forbidden_effects", "禁止产生的影响")]:
            values = "".join(f"<li>{text(v)}</li>" for v in group["body"][key])
            fields.append(f"<h3>{name}</h3><ul>{values}</ul>" if values else f"<h3>{name}</h3><p>未声明</p>")
        refs = ''.join('<li>' + (f'<a href="{text(titles[ref])}">{text(ref)}</a>' if ref in titles else text(ref)) + '</li>'
                       for ref in group['body'].get('refs', []))
        fields.append('<h3>来源引用</h3>' + ('<ul>' + refs + '</ul>' if refs else '<p>未声明</p>'))
        fields.append(fix_badges(data['workspace'], group['fix_tasks']))
        fields.append(''.join(f"<p><a href='/w/{text(data['workspace'])}/fixes?task={t['id']}'>所属修复任务 #{t['id']}</a></p>" for t in group['fix_tasks']))
        results = []
        for result in group["results"]:
            kind = "Agent" if result["kind"] == "agent" else "人工"
            extras = []
            for field, name in [("finding", "问题或缺陷"), ("note", "备注"), ("session", "会话")]:
                if result.get(field):
                    value = text(result[field])
                    if field == "finding" and result["kind"] == "manual":
                        value = defect_link(result[field])
                    extras.append(f"<p>{name}：{value}</p>")
            result_label = (MANUAL_STATUS if result["kind"] == "manual" else STATUS)[result["status"]]
            results.append(f"<article class='result'><h3>第 {result['number']} 轮 · {kind} · {result_label}</h3>"
                           f"<p>执行者：{text(result['executor'])} · 执行时间：{time_tag(result['executed_at'])}</p>"
                           f"<p>提交时间：{time_tag(result['submitted_at'])}</p>"
                           f"<p>实际结果：{text(result['actual'])}</p>{''.join(extras)}"
                           f"{''.join(evidence_preview(e) for e in result['evidence'])}</article>")
        sections.append(f"<section data-fingerprint='{text(group['fingerprint'])}'><h2>{label}</h2>"
                        f"<p class='fingerprint'>内容指纹：{text(group['fingerprint'])}</p>{''.join(fields)}"
                        f"<div data-result-history=''>{''.join(results) or '<p data-no-results>尚无执行结果</p>'}</div></section>")
    removed = "<p class='notice'>已从场景文件移除</p>" if not data["current"] else ""
    summary = data.get("summary")
    review = (f"<article class='scenario-card' data-scenario='{text(data['scenario'])}'>"
              f"{scenario_summary(summary)}{review_controls(data['workspace'], data['batch'], data)}</article>" if data["current"] else "")
    body = (f"<nav><a href='{text(base)}'>返回批次总览</a></nav>{batch_nav(data['workspace'], data['batch'], 'scenarios')}<h1>{text(data['scenario'])} · {text(data['title'])}</h1>"
            f"{removed}<p>节点：{text(data['node'])} · {'正向' if data['polarity'] == 'positive' else '反向'} · 优先级：{text(data['priority'])}</p>"
            f"<p>验证入口：{text(data['verify'])}</p>"
            f"<p>场景文件最新推送时间：{time_tag(data['pushed_at'])}</p>{identity_bar() if data['current'] else ''}"
            f"{review_completion(data['workspace'], data['batch'], data) if data['current'] else ''}{review}{''.join(sections)}")
    return layout(data["scenario"], body, watch=(data["workspace"], data["batch"], data["etag"]))


def identity_bar():
    return ("<section id='review-identity' class='identity-panel'><details data-disclosure='identity'>"
            "<summary><span>复核身份</span><span class='disclosure-hint'>标记人设置</span></summary>"
            "<div class='identity-content'><fieldset><legend>标记人（可随时更换）</legend>"
            "<label>标记人<input name='marker' autocomplete='off'></label>"
            "<button type='button' id='save-identity'>保存标记人</button></fieldset>"
            "<p id='identity-message' role='status' aria-live='polite'></p></div></details></section>")


def review_controls(workspace, name, item):
    action = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}/marks"
    buttons = "".join(f"<button type='button' data-mark-status='{value}' aria-expanded='false'>{label}</button>"
                      for value, label in [("pass", "通过"), ("fail", "失败"), ("blocked", "受阻")])
    return (f"<form class='review-form' action='{text(action)}' method='post' novalidate>"
            f"<input type='hidden' name='scenario' value='{text(item['scenario'])}'>"
            f"<input type='hidden' name='fingerprint' value='{text(item['fingerprint'])}'>"
            f"<div class='review-actions'>{buttons}</div><div data-review-fields='' hidden>"
            "<label>缺陷链接（必填）<input name='defect_url' type='text' required></label>"
            "<label>备注（选填）<textarea name='note' rows='2'></textarea></label>"
            "<button type='submit'>提交</button> <button type='button' data-review-cancel=''>取消</button></div>"
            "<p data-review-message='' role='status' aria-live='polite'></p>"
            "<noscript>请启用 JavaScript 后复核。</noscript></form>")


QUEUE = {"pending": "待执行", "issues": "问题", "retest": "复测", "reviewed": "已复核通过"}
FILTERS = [("all", "全部"), ("unreviewed", "未人工复核"), ("agent-pass", "Agent 已通过"),
           ("fail", "不通过"), ("blocked", "受阻")]


def batch_nav(workspace, name, current):
    base = batch_url(workspace, name)
    return "<nav aria-label='批次导航'>" + "".join(
        f"<a href='{text(base + suffix)}'" + (" aria-current='page'" if key == current else "") + f">{label}</a>"
        for key, suffix, label in [("overview", "", "总览"), ("scenarios", "/scenarios", "SOP 场景"),
                                   ("cases", "/cases", "用例"), ("progress", "/progress", "进度"), ("merges", "/merges", "合并"),
                                   ("sop", "/sop", "SOP"), ("draft", "/draft", "草稿")]) + "</nav>"


def sop_diff(before, after):
    old, new = before.splitlines(), after.splitlines()
    rows = []

    def row(kind, left, right, value):
        marker = {'context': '', 'delete': '−', 'insert': '+'}[kind]
        rows.append(f'<tr data-kind="{kind}"><td>{left}</td><td>{right}</td><td>{marker}</td>' +
                    f'<td><code>{text(value)}</code></td></tr>')

    for kind, a, b, c, d in SequenceMatcher(None, old, new).get_opcodes():
        if kind == 'equal':
            for offset, value in enumerate(old[a:b]):
                row('context', a + offset + 1, c + offset + 1, value)
        else:
            for offset, value in enumerate(old[a:b]):
                row('delete', a + offset + 1, '', value)
            for offset, value in enumerate(new[c:d]):
                row('insert', '', c + offset + 1, value)
    return ('<div class="table-scroll"><table class="sop-diff"><thead><tr><th>旧行</th><th>新行</th>' +
            '<th>变化</th><th>内容</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def sop(workspace, name, sources, query=None):
    query = query or {}
    content = batch_nav(workspace, name, 'sop') + f'<h1>批次 {text(name)} · SOP</h1>'
    if not sources:
        content += '<p>尚无 SOP 原文</p>'
    for path, versions in sources.items():
        rendered, _ = render_sop(versions[0]['content'], path)
        content += f'<section data-sop-path="{text(path)}"><h2>{text(path)}</h2>'
        content += f'<p>来源：{text(versions[0]["ref"])}</p><article class="sop-content">{rendered}</article>'
        content += ('<h3>版本记录</h3><div class="table-scroll"><table class="sop-versions"><thead><tr>' +
                    '<th>版本</th><th>提交号</th><th>SHA-256 前 12 位</th><th>确认人</th><th>登记时间</th></tr></thead><tbody>')
        for version in versions:
            content += (f'<tr data-version="{version["version"]}"><th scope="row">{version["version"]}</th>' +
                        f'<td>{text(version["commit_id"])}</td><td>{text(version["sha256"][:12])}</td>' +
                        f'<td>{text(version["confirmed_by"])}</td><td>{time_tag(version["recorded_at"])}</td></tr>')
        content += '</tbody></table></div>'
        if len(versions) > 1:
            content += (f'<form method="get" action="{text(batch_url(workspace, name) + "/sop")}">' +
                        f'<input type="hidden" name="path" value="{text(path)}">')
            for key, label, default in [('from', '旧版本', versions[1]['version']), ('to', '新版本', versions[0]['version'])]:
                selected = query.get(key, str(default)) if query.get('path') == path else str(default)
                options = ''.join(f'<option value="{v["version"]}"' +
                                  (' selected' if str(v['version']) == selected else '') +
                                  f'>版本 {v["version"]}</option>' for v in versions)
                content += f'<label>{label}<select name="{key}">{options}</select></label>'
            content += '<button type="submit">比较版本</button></form>'
        if query.get('path') == path:
            chosen = {str(v['version']): v for v in versions}
            before, after = chosen.get(query.get('from')), chosen.get(query.get('to'))
            if before and after and before['version'] != after['version']:
                content += f'<h3>版本 {before["version"]} → {after["version"]} 的逐行差异</h3>'
                content += sop_diff(before['content'], after['content'])
            else:
                content += '<p>请选择这份原文的两个不同版本。</p>'
        content += '</section>'
    return layout('SOP · ' + name, content, scripts=('/assets/vendor/mermaid/mermaid.min.js', '/assets/sop.js'))


def progress(workspace, data, coverage):
    def metric(key, label, value):
        return f"<div id='metric-{key}'><dt>{label}</dt><dd>{value}</dd></div>"

    development = metric("pending", "待执行", data["queue_counts"]["pending"])
    for key, label in [("executed_progress", "已执行进度"), ("agent_pass_rate", "Agent 验收通过率"), ("first_pass_rate", "一次通过率")]:
        development += metric(key, label, ratio(data["statistics"][key]))
    testing = metric("retest", "待复核", data["queue_counts"]["retest"])
    for key, label in [("reviewed_progress", "已复核进度"), ("manual_pass_rate", "人工复测通过率")]:
        testing += metric(key, label, ratio(data["statistics"][key]))
    testing += metric("misjudgments", "误判", len(data["misjudgments"]))
    content = (batch_nav(workspace, data["name"], "progress") + f"<h1>批次 {text(data['name'])} · 进度</h1>"
               f"<div class='progress-columns'><section><h2>开发侧</h2><dl>{development}</dl></section>"
               f"<section><h2>测试侧</h2><dl>{testing}</dl></section></div>"
               f"<dl>{metric('coverage', '用例覆盖', ratio(coverage))}</dl>")
    return layout("进度 · " + data["name"], content, watch=(workspace, data["name"], data["etag"]))


def cases(workspace, name, data):
    base = batch_url(workspace, name)
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}"
    content = (batch_nav(workspace, name, "cases") + f"<h1>批次 {text(name)} · 用例</h1>" + identity_bar() +
               f"<form id='cases-upload' action='{text(api + '/cases')}' method='post' novalidate>"
               "<label>选择用例 YAML 文件<input name='file' type='file' accept='.yaml,.yml,text/yaml'></label>"
               "<button type='submit'>上传用例</button><p data-cases-message='' role='status' aria-live='polite'></p>"
               "<ul data-cases-problems='' role='alert'></ul><noscript>请启用 JavaScript 后上传。</noscript></form>"
               f"<p>用例覆盖：{ratio(data['coverage'])}</p>" +
               (f"<a href='{text(api + '/cases.yaml')}'>下载用例并集</a>" if data["uploads"] else ""))
    rows = "".join(f"<tr data-upload='{u['upload']}'><th scope='row'>{u['upload']}</th>"
                   f"<td>{text(u['filename'])}</td><td>{text(u['uploaded_by'])}</td><td>{time_tag(u['uploaded_at'])}</td>"
                   f"<td>{u['case_count']}</td><td>{u['added']}</td><td>{u['updated']}</td></tr>" for u in data["uploads"])
    content += ("<section><h2>上传记录</h2><div class='table-scroll'><table><thead><tr><th>序号</th><th>文件名</th>"
                "<th>上传人</th><th>上传时间</th><th>条数</th><th>新增</th><th>更新</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div></section>")
    pairings = {p["case"]: p for p in data["pairings"]}
    groups = {}
    types = dict(functional="功能", ui="界面", security="安全", performance="性能", accessibility="无障碍")
    for item in data["cases"]:
        group = next((tag[len("section:"):] for tag in item["tags"] if tag.startswith("section:") and tag[len("section:"):].strip()), "未分组")
        groups.setdefault(group, []).append(item)
    for group in [g for g in groups if g != "未分组"] + (["未分组"] if "未分组" in groups else []):
        content += f"<section class='case-group'><h2>{text(group)}</h2>"
        for item in groups[group]:
            content += (f"<article data-case='{text(item['id'])}'><h3>{text(item['id'])} · {text(item['title'])}</h3>"
                        f"<p>优先级：{text(item['priority'])} · 类型：{types[item['type']]}</p>")
            for field, label in [("preconditions", "前置条件"), ("steps", "步骤"), ("expected_results", "预期")]:
                values = "".join(f"<li>{text(v)}</li>" for v in item.get(field, []))
                content += f"<h4>{label}</h4><ul>{values}</ul>" if values else f"<h4>{label}</h4><p>未声明</p>"
            pair = pairings.get(item['id'])
            if pair:
                target = pair['scenarios'][0]
                label = '细节用例并入 ' if pair['kind'] == 'detail' else '流程场景 '
                content += f"<a href='{text(base + '/s/' + quote(target, safe=''))}'>{text(label + target)}</a><p>{text(pair['kind_reason'])}</p>"
            else:
                content += '<p>尚无当前用例的有效配对，等待合并</p>'
            content += "</article>"
        content += "</section>"
    if not groups:
        content += "<p>尚无用例，请选择文件上传。</p>"
    return layout("用例 · " + name, content, watch=(workspace, name, data["etag"]))


DRAFT_BASIS = dict(sop='SOP 依据', code='代码推理', annotation='批注确认')
ANNOTATION_KINDS = dict(comment='修改意见', remove='建议删除用例', add='补充场景', answer='回答问题')


def draft_annotation_form(api, version, target):
    kinds = ['comment', 'add']
    if target.startswith('case:'):
        kinds.append('remove')
    if target.startswith('question:'):
        kinds.append('answer')
    options = ''.join(f"<option value='{kind}'>{ANNOTATION_KINDS[kind]}</option>" for kind in kinds)
    return (f"<details class='draft-feedback' data-disclosure='annotation-{text(target)}'><summary>添加批注</summary>"
            f"<form data-draft-form='annotation' action='{text(api)}/annotations' method='post' novalidate>"
            f"<input type='hidden' name='version' value='{version}'>"
            f"<input type='hidden' name='target' value='{text(target)}'>"
            f"<label>类型<select name='kind'>{options}</select></label>"
            "<label>批注内容<textarea name='text' rows='3' required></textarea></label>"
            "<button type='submit'>提交批注</button> <button type='button' data-draft-cancel='' class='button-secondary'>取消</button>"
            "<p data-draft-message='' role='status' aria-live='polite'></p></form></details>")


def draft_case_text(item):
    labels = [('title', '标题'), ('priority', '优先级'), ('type', '类型'), ('description', '说明'),
              ('preconditions', '前置条件'), ('steps', '步骤'), ('expected_results', '预期'),
              ('tags', '标签'), ('auto', '自动执行'), ('rules', '依据规则'), ('basis', '依据来源'), ('basis_note', '依据说明')]
    lines = []
    for key, label in labels:
        value = item.get(key)
        if isinstance(value, list):
            value = '、'.join(value)
        elif key == 'basis' and value is not None:
            value = DRAFT_BASIS[value]
        elif type(value) is bool:
            value = '是' if value else '否'
        lines.append(label + '：' + str(value if value is not None else '未声明'))
    return '\n'.join(lines)


def draft_changes(previous, current, *, registered_cases=()):
    old = {c['id']: c for c in (previous['output']['cases'] if previous else registered_cases)}
    new = {c['id']: c if previous else generated_case(c) for c in current['output']['cases']}
    groups = [('新增', [cid for cid in new if cid not in old]),
              ('修改', [cid for cid in new if cid in old and old[cid] != new[cid]]),
              ('未变', [cid for cid in new if cid in old and old[cid] == new[cid]])]
    if previous and old.keys() - new.keys():
        groups.append(('删除', [cid for cid in old if cid not in new]))
    heading = f"v{previous['number']} → v{current['number']}" if previous else '首版 · 对比已登记用例'
    content = f'<section><h2>版本差异 · {heading}</h2>'
    for label, ids in groups:
        content += f'<h3>{label}（{len(ids)}）</h3><ul>'
        for cid in ids:
            title = text(cid + ' · ' + (new.get(cid) or old[cid])['title'])
            content += '<li>' + (f"<a href='#case-{text(cid)}'>{title}</a>" if cid in new else title)
            if label == '修改':
                content += '<details><summary>查看变化</summary>' + sop_diff(draft_case_text(old[cid]), draft_case_text(new[cid])) + '</details>'
            content += '</li>'
        content += '</ul>'
    return content + '</section>'


def draft(workspace, name, data, selected=None, *, local=False):
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}/draft"
    content = batch_nav(workspace, name, 'draft') + f'<h1>批次 {text(name)} · 草稿</h1>' + identity_bar()
    row = data['draft']
    if not row:
        return layout('草稿 · ' + name, content + '<p>尚无草稿，请先上传业务 SOP。</p>', watch=(workspace, name, data['etag']))
    editable = row['status'] not in ('已确认', '已放弃')
    content += f"<p class='execution-banner'>草稿状态：{text(row['status'])}</p>"
    task = row['current_task']
    if task:
        content += (f"<section><h2>生成 / 修订任务 #{task['id']} · {text(task['status'])}</h2>"
                    f"<p>{text(task['trigger'])} · {time_tag(task['created_at'])}</p><p>{text(task['error'])}</p>")
        problems = task['result'].get('problems', [])
        if problems:
            content += '<ul>' + ''.join('<li>' + text(p) + '</li>' for p in problems) + '</ul>'
        if local and task.get('session_id') and not row['versions']:
            content += '<p>查阅生成会话：<code>' + text('codex resume ' + task['session_id']) + '</code></p>'
        if local and editable:
            if task['status'] in ('失败', '已中断', 'Codex 不可用'):
                content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/retry'>重试</button> "
            if task['status'] in ('排队中', '运行中', '已中断'):
                content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/cancel'>放弃</button>"
        content += "<p id='developer-message' role='status' aria-live='polite'></p></section>"
    versions = row['versions']
    if not versions:
        return layout('草稿 · ' + name, content + '<p>尚无可查看的草稿版本。</p>', watch=(workspace, name, data['etag']))
    current = next((v for v in versions if str(v['number']) == selected), versions[-1])
    output = current['output']
    previous = next((v for v in versions if v['number'] == current['number'] - 1), None)
    counts = {basis: sum(c['basis'] == basis for c in output['cases']) for basis in DRAFT_BASIS}
    content += '<dl class="metrics">' + ''.join(f"<div><dt>{label}</dt><dd>{counts[basis]}</dd></div>" for basis, label in DRAFT_BASIS.items()) + '</dl>'
    options = ''.join(f"<option value='{v['number']}'{' selected' if v == current else ''}>v{v['number']}</option>" for v in versions)
    content += f"<form method='get'><label>草稿版本<select name='version'>{options}</select></label><button type='submit'>查看版本</button></form>"
    content += f"<p>当前查看 v{current['number']} · {time_tag(current['created_at'])} · {text(current['revision_note'])}</p>"
    if local and current.get('thread_id'):
        content += '<p>查阅生成会话：<code>' + text('codex resume ' + current['thread_id']) + '</code></p>'
    confirmation = row['confirmation']
    if confirmation:
        content += (f"<section><h2>确认记录 · v{confirmation['version']}</h2><p>{text(confirmation['confirmed_by'])} · {time_tag(confirmation['confirmed_at'])}</p>"
                    f"<p><a href='{text(batch_url(workspace, name))}/cases'>用例上传 #{confirmation['upload_number']}</a></p>"
                    f"<p>确认时未处理批注：{len(confirmation['pending_annotations'])} 条 · 确认时未回答问题：{len(confirmation['unanswered_questions'])} 条</p></section>")
    if editable:
        content += (f"<section data-draft-actions='{text(api)}' data-version='{current['number']}'>"
                    "<h2>草稿操作</h2><button type='button' data-draft-revise=''>按反馈修订</button> "
                    "<button type='button' data-draft-confirm=''>确认草稿</button>"
                    "<p data-draft-message='' role='status' aria-live='polite'></p>" +
                    draft_annotation_form(api, current['number'], 'draft') + '</section>')
        pending = sum(a['status'] == '待处理' for a in row['annotations'])
        content += ("<dialog id='draft-confirm-dialog' aria-labelledby='draft-confirm-title'><h2 id='draft-confirm-title'>确认草稿</h2>"
                    f"<p>待处理批注 {pending} 条 · 待回答问题 {len(output['questions'])} 条 · 代码推理用例 {counts['code']} 条</p>"
                    "<p>仍然确认后，用例会登记上传并进入合并；未处理项保留在确认记录中。尚未提交的批注不会保存。</p>"
                    "<button type='button' data-draft-confirm-submit=''>仍然确认</button> "
                    "<button type='button' data-draft-confirm-cancel='' class='button-secondary'>取消</button>"
                    "<p data-draft-message='' role='status' aria-live='polite'></p></dialog>")
    content += draft_changes(previous, current, registered_cases=row['registered_cases'])
    coverage = {c['rule']: c for c in output['coverage']}
    content += "<section><h2>规则覆盖矩阵</h2><div class='table-scroll'><table><thead><tr><th>规则与原文</th><th>正向用例</th><th>负向用例 / 豁免</th><th>批注</th></tr></thead><tbody>"
    for rule in output['rules']:
        cover = coverage[rule['id']]
        content += f"<tr id='rule-{text(rule['id'])}'><th>{text(rule['id'])} · {text(rule['section'])}<p>{text(rule['excerpt'])}</p></th>"
        for polarity in ('positive', 'negative'):
            content += '<td>' + str(len(cover[polarity])) + ' 条：' + text('、'.join(cover[polarity]) or '无')
            if polarity == 'negative' and cover['negative_exempt']:
                content += "<p class='notice'><strong>负向豁免：</strong>" + text(cover['negative_exempt']) + '</p>'
            content += '</td>'
        content += '<td>' + (draft_annotation_form(api, current['number'], 'rule:' + rule['id']) if editable else '') + '</td></tr>'
    content += '</tbody></table></div></section><section><h2>用例列表</h2>'
    types = dict(functional='功能', ui='界面', security='安全', performance='性能', accessibility='无障碍')
    for item in output['cases']:
        content += (f"<article class='scenario-card draft-case' id='case-{text(item['id'])}' data-basis='{text(item['basis'])}'>"
                    f"<h3>{text(item['id'])} · {text(item['title'])}</h3>"
                    f"<p><span class='draft-basis'>{DRAFT_BASIS[item['basis']]}</span> {text(item['basis_note'])}</p>"
                    f"<p>依据：{text('、'.join(item['rules']))} · 优先级：{text(item['priority'])} · 类型：{types[item['type']]}</p>"
                    f"<p>{text(item.get('description') or '')}</p>")
        for field, label in [('preconditions', '前置条件'), ('steps', '步骤'), ('expected_results', '预期')]:
            content += f'<h4>{label}</h4><ul>' + ''.join('<li>' + text(v) + '</li>' for v in item.get(field) or []) + '</ul>'
        content += '<p>标签：' + text('、'.join(item['tags'])) + '</p>'
        if editable:
            content += draft_annotation_form(api, current['number'], 'case:' + item['id'])
        content += '</article>'
    content += '</section><section><h2>待回答问题</h2>'
    for question in output['questions']:
        unresolved = confirmation and confirmation['version'] == current['number']
        content += (f"<article><h3>{text(question['id'])} · {'确认时未回答' if unresolved else '待回答'}</h3>"
                    f"<p>{text(question['text'])}</p><p>关联规则：{text(question['rule'] or '整份草稿')}</p>")
        if editable:
            content += draft_annotation_form(api, current['number'], 'question:' + question['id'])
        content += '</article>'
    if not output['questions']:
        content += '<p>无待回答问题</p>'
    content += '</section>'
    for title, values in [('假设', [a['text'] + ' · 用例：' + '、'.join(a['cases']) for a in output['assumptions']]),
                           ('过时建议', [a['case'] + '：' + a['reason'] for a in output['obsolete']]), ('备注', output['notes'])]:
        content += f'<section><h2>{title}</h2>' + ('<ul>' + ''.join('<li>' + text(v) + '</li>' for v in values) + '</ul>' if values else '<p>无</p>') + '</section>'
    content += '<section><h2>批注列表</h2>'
    for a in row['annotations']:
        target = a['target']
        target_label = '整份草稿' if target == 'draft' else {'case': '用例', 'rule': '规则', 'question': '问题'}[target.partition(':')[0]] + ' ' + target.partition(':')[2]
        content += (f"<article class='scenario-card' id='annotation-{a['number']}'><h3>#{a['number']} · {text(a['status'])}</h3>"
                    f"<p>{text(target_label)} · {ANNOTATION_KINDS[a['kind']]} · 作者：{text(a['author'])} · <a href='?version={a['version']}'>v{a['version']}</a> · {time_tag(a['created_at'])}</p>"
                    f"<p class='draft-text'>{text(a['text'])}</p><p>处理说明：{text(a['processing_note'] or '尚未处理')}"
                    + (f" · 所在版本 v{a['processed_version']}" if a['processed_version'] else '') + '</p>')
        if editable and a['status'] == '待处理':
            content += (f"<details class='draft-feedback' data-disclosure='reject-{a['number']}'><summary>标为未采纳</summary>"
                        f"<form data-draft-form='reject' action='{text(api)}/annotations/{a['number']}/reject' method='post' novalidate>"
                        "<label>理由<textarea name='reason' required></textarea></label><button type='submit'>标为未采纳</button> "
                        "<button type='button' data-draft-cancel='' class='button-secondary'>取消</button>"
                        "<p data-draft-message='' role='status' aria-live='polite'></p></form></details>")
        content += '</article>'
    if not row['annotations']:
        content += '<p>暂无批注</p>'
    content += '</section><section><h2>修订历史</h2><ul>'
    for version in versions:
        content += f"<li>v{version['number']} · {time_tag(version['created_at'])} · {text(version['revision_note'])}</li>"
    for event in row['history']:
        content += f"<li>v{event['version']} · {time_tag(event['created_at'])} · {text(event['author'])} 手动将 #{event['annotation']} 标为未采纳：{text(event['note'])}</li>"
    return layout('草稿 · ' + name, content + '</ul></section>', watch=(workspace, name, data['etag']))


def scenario_fields(body):
    parts = []
    for key, label in [("initial_state", "初始状态"), ("steps", "操作步骤"), ("expected", "预期结果"),
                       ("forbidden_effects", "禁止产生的影响"), ("refs", "来源引用")]:
        values = "".join(f"<li>{text(v)}</li>" for v in body.get(key, []))
        parts.append(f"<h3>{label}</h3><ul>{values}</ul>" if values else f"<h3>{label}</h3><p>未声明</p>")
    return "".join(parts)


def latest_result(result, kind):
    if not result:
        return ""
    link = defect_link(result['finding']) if kind == "人工" and result.get("finding") else ""
    note = f"<p>备注：{text(result['note'])}</p>" if result.get("note") else ""
    label = (MANUAL_STATUS if kind == "人工" else STATUS)[result["status"]]
    return (f"<p>{kind} · {label} · {'标记人' if kind == '人工' else '执行者'}：{text(result['executor'])} · "
            f"{time_tag(result['executed_at'])}</p>{link}{note}")


def scenario_summary(item):
    return (f"<div data-latest-agent=''>{latest_result(item['latest_agent'], 'Agent')}</div>"
            f"<div data-latest-manual=''>{latest_result(item['latest_manual'], '人工')}</div>"
            f"<p data-queue=''>所在队列：{QUEUE[item['queue']]}</p>")


def fix_badges(workspace, tasks):
    return ''.join(f"<p><a href='/w/{text(workspace)}/fixes?task={t['id']}'>{'待开发者处理' if t['category'] not in ('', 'product') or t['status'] == '待开发者处理' else '修复中'} #{t['id']}</a></p>"
        for t in tasks if t['status'] not in ('已合入', '已放弃'))


def scenarios(workspace, name, data, *, local=False):
    base = batch_url(workspace, name)
    by_id = {item["scenario"]: item for item in data["scenarios"]}
    content = batch_nav(workspace, name, "scenarios") + f"<h1>批次 {text(name)} · SOP 场景</h1>"
    content += execution_controls(workspace, name, data, local=local) + identity_bar()
    content += review_completion(workspace, name, data)
    if not data["has_version"]:
        content += f"<p>尚无场景，测试岗上传用例后由合并生成</p><a href='{text(base + '/cases')}'>用例页</a>"
        return layout("SOP 场景 · " + name, content, watch=(workspace, name, data["etag"]))
    content += "<nav aria-label='场景筛选'>" + "".join(
        f"<a href='?filter={key}' data-filter='{key}'>{label} <span>{data['filters'][key]}</span></a>"
        for key, label in FILTERS) + "</nav>"
    for node in data["nodes"]:
        content += (f"<section class='scenario-node' data-node='{text(node['id'])}'><h2>{text(node['id'])} · {text(node['title'])}</h2>"
                    f"<p>角色：{text(node['actor'])} · 入口：{text(node['entry'])}</p>")
        for source in node["scenarios"]:
            item = by_id[source["id"]]
            content += (f"<article class='scenario-card' data-scenario='{text(source['id'])}' "
                        f"data-status='{text(item['status'] or '')}' data-agent-pass='{str(bool(item['latest_agent'] and item['latest_agent']['status'] == 'pass')).lower()}' "
                        f"data-unreviewed='{str(item['latest_manual'] is None).lower()}'>"
                        f"<h3><a href='{text(base + '/s/' + quote(source['id'], safe=''))}'>{text(source['id'])}</a> · {text(source['title'])}</h3>"
                        f"<p>{'正向' if source['polarity'] == 'positive' else '反向'} · 优先级：{text(source['priority'])} · 角色：{text(source['actor'])} · 验证入口：{text(source['verify'])}</p>"
                        f"{fix_badges(workspace, item['fix_tasks'])}{scenario_fields(source)}{scenario_summary(item)}{review_controls(workspace, name, item)}</article>")
        content += "</section>"
    return layout("SOP 场景 · " + name, content, watch=(workspace, name, data["etag"]))


def merges(workspace, name, tasks, case_data, selected=None, *, local=False):
    api = f"/api/v1/workspaces/{quote(workspace, safe='')}/batches/{quote(name, safe='')}"
    content = (batch_nav(workspace, name, 'merges') + f"<h1>批次 {text(name)} 的任务</h1>" + identity_bar() +
               "<p>合并结果写入配置的场景目录。待同步时请核对工作区的修改或配置变化。</p>" +
               (f"<button type='button' data-developer-action='{text(api + '/merges')}'>重新合并</button>" if local else "") +
               "<p id='developer-message' role='status' aria-live='polite'></p>")
    groups = {}
    for pair in case_data['pairings']:
        groups.setdefault(pair['scenarios'][0], []).append(pair)
    choices = set(case_data['flow_choices'])
    content += '<section><h2>当前流程场景与用例归属</h2>'
    for scenario, pairs in sorted(groups.items()):
        content += f"<h3>流程场景 {text(scenario)}</h3><ul>"
        for pair in sorted(pairs, key=lambda p: (p['kind'] != 'flow', p['case'])):
            label = '并入的细节用例' if pair['kind'] == 'detail' else '流程用例'
            content += f"<li>{text(label)} {text(pair['case'])}：{text(pair['kind_reason'])}"
            if pair['case'] in choices:
                content += '<span> · 已指定单独成场景' + ('，等待重新合并' if pair['kind'] == 'detail' else '') + '</span>'
            elif local and pair['kind'] == 'detail':
                action = api + '/cases/' + quote(pair['case'], safe='') + '/flow'
                content += f" <button type='button' data-case-flow='' data-developer-action='{text(action)}'>改为单独成场景</button>"
            content += '</li>'
        content += '</ul>'
    if not groups:
        content += '<p>尚无有效配对，请完成合并。</p>'
    content += '</section>'
    current = next((t for t in tasks if str(t['id']) == selected), next((t for t in reversed(tasks) if t['kind'] == 'merge'), None))
    content += "<table><thead><tr><th>任务</th><th>触发原因</th><th>状态</th><th>创建时间</th></tr></thead><tbody>"
    for task in tasks:
        content += (f"<tr><td><a href='?task={task['id']}'>#{task['id']}</a></td><td>{text(task['trigger'])}</td>" +
                    f"<td>{text(task['status'])}</td><td>{time_tag(task['created_at'])}</td></tr>")
    content += '</tbody></table>'
    if current:
        task = current
        content += (f"<section><h2>任务 #{task['id']} · {text(task['status'])}</h2>" +
                    f"<p>{text(task['trigger'])} · {time_tag(task['created_at'])}</p>" +
                    f"<p>{text(task['error'])}</p>")
        result = task['result']
        content += '<h3>用例到场景</h3><ul>'
        for pair in result.get('pairs', []):
            content += f"<li>{text(pair['case'])} → {text('、'.join(pair['scenarios']))} · {text('细节' if pair['kind'] == 'detail' else '流程')} · {text(pair['kind_reason'])}</li>"
        content += '</ul><h3>原有场景的去向</h3><ul>'
        for item in result.get('scenarios', []):
            action = {'kept': '保留', 'duplicate': '重复', 'conflict': '冲突'}[item['action']]
            content += f"<li>{text(item['id'])}：{action} {text(item.get('target', ''))}；{text(item.get('reason', ''))}</li>"
        content += '</ul><h3>待确认的推断</h3><ul>'
        for item in result.get('inferences', []):
            content += f"<li>{text(item['id'])}：{text(item['content'])}</li>"
        content += '</ul><h3>缺口</h3><ul>'
        for gap in result.get('gaps', []):
            content += f'<li>{text(gap)}</li>'
        content += '</ul>'
        if result.get('problems'):
            content += '<h3>核对问题（check.json）</h3><pre>' + text(json.dumps(result['problems'], ensure_ascii=False, indent=2)) + '</pre>'
        if task['kind'] == 'merge' and task['status'] in ('已完成', '待同步'):
            content += f"<p><a href='{text(api)}/merges/{task['id']}/scenario-file' download>下载合并场景</a> " + f"<a href='{text(api)}/merges/{task['id']}/pairs.json' download>下载配对文件</a></p>"
        for name_, url in task['files'].items():
            content += f"<p><a href='{text(url)}' download>{text(name_)}</a></p>"
        if local and task['status'] in ('失败', '已中断', 'Codex 不可用', '已作废'):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/retry'>重试</button>"
        if local and task['status'] in ('排队中', '运行中', '已中断', '待同步', '待补充', '待提交', '等待浏览器', '等待合并'):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/cancel'>放弃</button>"
        content += '</section>'
    return layout('任务', content)


def fixes(workspace, tasks, selected=None, *, review=None, local=False):
    content = ("<h1>修复任务</h1>" + identity_bar() +
               "<p id='developer-message' role='status' aria-live='polite'></p>")
    content += (f"<section data-fix-state='/api/v1/workspaces/{quote(workspace, safe='')}/fixes'>" +
                fix_waiting(review or {}) + "</section>")
    if not tasks:
        return layout('修复任务', content + '<p>暂无记录</p>')
    content += '<table><tr><th>任务</th><th>问题数</th><th>状态与原因</th><th>尝试次数</th><th>会话编号</th></tr>'
    for task in tasks:
        content += (f"<tr><td><a href='?task={task['id']}'>#{task['id']}</a></td><td>{len(task['problems'])}</td>" +
            f"<td>{text(task['status'])} · {text(task['error'])}</td><td>{task['attempt']}</td><td>{text(task['session_id'] or '')}</td></tr>")
    content += '</table>'
    task = next((t for t in tasks if str(t['id']) == selected), tasks[-1])
    content += f"<h2>修复任务 #{task['id']}</h2><h3>根因分析</h3><p>{text(task['result'].get('root_cause', ''))}</p><h3>问题与处理</h3><ul>"
    categories = dict(product='产品缺陷', execution='执行问题', environment='环境问题', scenario='场景问题', requirement='需求问题', unfixable='暂无法修复的产品缺陷')
    for problem in task['problems']:
        content += (f"<li>{text(problem['batch'])} {text(problem['scenario'])}：{text(categories.get(problem['category'], '待分析'))} · " +
            f"根因仓库：{text(problem['root_repo'] or '未定位')} · {text(problem['handling'])} · {text(problem['reason'])} · {text(problem['plan'])}" +
            f"<p>{defect_link(problem['input'].get('defect_url'))}</p>" +
            f"<p>{text(problem['input'].get('jira_error', ''))}</p></li>")
    content += '</ul><h3>各仓改动与合入</h3><table><tr><th>仓库</th><th>改动文件</th><th>修复提交</th><th>基线与合入</th><th>状态与原因</th></tr>'
    for repo in task['repositories']:
        content += (f"<tr><td>{text(repo['alias'])} · {text(repo['identity'])}</td><td>{text('、'.join(repo['changed_files']) or '无')}</td>"
            f"<td>{text(repo['repair_commit'])}</td><td>{text(repo['baseline_branch'])} · {text(repo['merged_commit'] or '未合入')}</td>"
            f"<td>{text(repo['merge_status'])} · {text(repo['merge_reason'])}</td></tr>")
    content += '</table><h3>尝试记录</h3>'
    for attempt in task['attempts']:
        commits = '；'.join(alias + '：' + commit for alias, commit in attempt['commits'].items()) or '本次无新提交'
        merged = '；'.join(alias + '：' + commit for alias, commit in attempt['merged_commits'].items()) or '未合入'
        evidence = ''.join(f"<p><a href='{text(url)}'>{text(name)}</a></p>"
            for name, url in task['files'].items()
            if any(path.endswith('/' + name) for path in attempt['validation'].get('evidence', [])))
        content += (f"<section><h4>第 {attempt['number']} 次 · {text(attempt['outcome'])}</h4><p>{text(attempt['reason'])}</p>" +
            f"<p>逐仓提交：{text(commits)}</p><p>逐仓合入：{text(merged)}</p><p>{text(attempt['result'].get('root_cause', ''))}</p>" +
            evidence + '<pre>' + text(json.dumps(attempt['validation'], ensure_ascii=False, indent=2)) + '</pre></section>')
    content += '<h3>待开发者处理事项</h3><ul>' + ''.join(
        f"<li>{text(p['batch'])} {text(p['scenario'])}：{text(p['reason'])} · {text(p['plan'])}</li>" for p in task['attention']) + '</ul>'
    content += '<h3>Jira 回写结果</h3><ul>' + ''.join(
        f"<li>{text(r['issue'])}：{text(r['comment'])} · {text(r['transition'])} · {text(r['reason'])}</li>" for r in task['jira']) + '</ul>'
    if local:
        content += "<label>开发者说明<textarea id='developer-note'></textarea></label>"
        if task['status'] == '待开发者处理':
            content += f"<button type='button' data-developer-note='' data-developer-action='/api/v1/tasks/{task['id']}/approve'>批准方案</button>"
        else:
            content += "<button type='button' disabled>批准方案</button>"
        if task['status'] in ('已中断', 'Codex 不可用') or (task['status'] == '待开发者处理' and task.get('retry_reason') in ('preparation', 'interrupted', 'git-identity')):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/retry'>重试</button>"
        else:
            content += "<button type='button' disabled>重试</button>"
        if task['status'] not in ('已合入', '已放弃', '待合入'):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/cancel'>放弃</button>"
        else:
            content += "<button type='button' disabled>放弃</button>"
    for name, url in task['files'].items():
        content += f"<p><a href='{text(url)}' download>{text(name)}</a></p>"
    return layout('修复任务', content)
