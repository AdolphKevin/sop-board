"""把 Service 的读取结果呈现为服务端 HTML，不重新计算业务指标。"""

from html import escape
from difflib import SequenceMatcher
import json
import re
from urllib.parse import quote

from .defect_links import ISSUE_KEY
from .time_format import local_time
from .sop_markdown import render as render_sop


def text(value):
    return escape(str(value), quote=True)


def batch_url(project, batch):
    return f"/p/{quote(project, safe='')}/b/{quote(batch, safe='')}"


def time_tag(value, element_id=None):
    identifier = f" id='{text(element_id)}'" if element_id else ""
    return f"<time{identifier} datetime='{text(value)}'>{local_time(value)}</time>"


def layout(title, content, jira_url="", watch=None, scripts=()):
    jira = f" data-jira-url='{text(jira_url)}'" if jira_url else ""
    polling = ""
    if watch:
        project, name, etag = watch
        api = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(name, safe='')}/state"
        polling = f" data-state-url='{text(api)}' data-etag='{text(etag)}'"
        content = "<p id='refresh-message' class='notice' role='status' aria-live='polite' hidden></p>" + content
    extra_scripts = ''.join(f"<script src='{text(src)}' defer></script>" for src in scripts)
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{text(title)} · sop-board</title>"
            "<link rel='stylesheet' href='/assets/board.css'><script src='/assets/board.js' defer></script>"
            f"{extra_scripts}</head><body>"
            "<header><a href='/'>sop-board</a><span>SOP 验证协作看板</span></header>"
            f"<main{jira}{polling}>{content}</main></body></html>")


def home(data):
    projects = []
    for project in data["projects"]:
        batches = "".join(f"<li data-batch='{text(batch['name'])}'><a href='{text(batch_url(project['key'], batch['name']))}'>"
                          f"{text(batch['name'])}</a> · <span data-agent-state=''>{agent_state(batch)}</span></li>" for batch in project["batches"])
        projects.append(f"<section><h2>{text(project['name'])}</h2><p>{text(project['id'])}</p>"
                        f"<p>{'可写' if project.get('writable') else '只读'}</p><ul>{batches}</ul>" +
                        ''.join(f"<p><a href='/p/{text(project['key'])}/fixes?task={t['id']}'>修复任务 #{t['id']}</a> · {text(t['status'])} · {text(t['error'])}</p>" for t in project.get('fix_tasks', [])) + '</section>')
    return layout("项目与批次", automation_controls(data) + "<h1>项目与批次</h1>" +
                  ("".join(projects) or "<p>还没有项目，推送场景文件后会显示在这里。</p>") + identity_bar())


def ratio(value):
    return f"{value['numerator']}/{value['denominator']}" if value["denominator"] else "无数据"


def agent_state(data):
    state = execution_state(data)
    return state + (' · 已暂停' if data.get('automation_paused') else '')


def execution_state(data):
    running = [t for t in data.get('execution_tasks', []) if t['kind'] == 'execute' and t['status'] == '运行中']
    if running:
        return f"执行中（{len(running)} 个会话，开始于 {local_time(running[0]['started_at'])}，任务 #{running[0]['id']}）"
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


def automation_controls(data):
    paused = data.get('automation_paused', False)
    notice = "<p id='automation-state' role='status'>自动任务已暂停</p>" if paused else "<p id='automation-state' role='status' hidden></p>"
    return ("<section>" + notice + f"<button type='button' data-automation-toggle='' data-developer-action='/api/v1/automation/{'resume' if paused else 'pause'}'>"
            f"{'恢复自动任务' if paused else '暂停自动任务'}</button>"
            "<p id='developer-message' role='status' aria-live='polite'></p></section>")


def execution_list(data):
    if not data.get('execution_tasks'):
        return ''
    rows = ''.join(f"<tr><th>#{t['id']}</th><td>{'执行' if t['kind'] == 'execute' else '检查'}</td>"
        f"<td>{text(t['trigger'])}</td><td>{len(t['scope'])}</td><td>{text(t['status'])}</td>"
        f"<td>{time_tag(t['started_at']) if t['started_at'] else ''}</td>"
        f"<td>{time_tag(t['finished_at']) if t['finished_at'] else ''}</td>"
        f"<td>{text(t['void_reason'] or t['error'])}</td><td>{text('、'.join('第 '+str(n)+' 轮' for n in t['rounds']))}</td></tr>"
        for t in data['execution_tasks'])
    return ('<section><h2>执行任务</h2><div class="table-scroll"><table><thead><tr><th>编号</th><th>种类</th>'
        '<th>触发原因</th><th>范围条数</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>原因</th><th>轮次</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table></div></section>')


def batch(project, data):
    base = batch_url(project, data["name"])
    api = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(data['name'], safe='')}"
    workspace = f"<p>{'可写' if data.get('writable') else '只读'}</p>"
    if data.get('entry_problems'):
        workspace += '<section><h2>入口声明不合格</h2>' + ''.join(
            f"<p>{text(p['problem'])}</p>" for p in data['entry_problems']) + '</section>'
    if data.get("workspace_problems"):
        workspace += "<section><h2>工作区场景文件不合格</h2>" + "".join(
            f"<p>{text(p.get('path', ''))} 第 {p['line']} 行：{text(p['field'])} {text(p['problem'])}</p>"
            for p in data["workspace_problems"]) + "</section>"
    report_link = workspace + execution_list(data) + f"<p><a href='{text(api + '/report.md')}'>导出报告</a></p>"
    if data.get('writable'):
        report_link += (f"<p><button type='button' data-developer-action='{text(api)}/executions'>执行待执行场景</button> "
                        f"<button type='button' data-developer-action='{text(api)}/checks'>运行回归检查</button></p>")
    report_link += automation_controls(data) + identity_bar()
    if data["version"] is None:
        return layout("批次 " + data["name"], batch_nav(project, data["name"], "overview") +
                      f"<h1>批次 {text(data['name'])}</h1><p>尚无场景，测试岗上传用例后由合并生成</p>"
                      f"<p id='agent-state'>{agent_state(data)}</p>{report_link}"
                      f"<a href='{text(base + '/cases')}'>用例页</a>", watch=(project, data["name"], data["etag"]))
    metrics = "".join(f"<div id='metric-{key}'><dt>{label}</dt><dd>{ratio(data['statistics'][key])}</dd></div>"
                      for key, label in [("agent_pass_rate", "Agent 验收通过率"), ("manual_pass_rate", "人工复测通过率"),
                      ("retest_coverage", "复测覆盖率"), ("first_pass_rate", "一次通过率"),
                      ("executed_progress", "已执行进度"), ("reviewed_progress", "已复核进度")])
    queues = []
    for key, label in [("pending", "待执行"), ("issues", "问题"), ("retest", "复测")]:
        links = "".join(f"<li><a data-scenario='{text(s)}' href='{text(base + '/s/' + quote(s, safe=''))}'>{text(s)}</a></li>"
                        for s in data["queues"][key])
        queues.append(f"<section id='queue-{key}'><h2>{label} <span data-count='{key}'>{data['queue_counts'][key]}</span></h2>"
                      f"<ul>{links}</ul>" + ("" if links else "<p>暂无场景</p>") + "</section>")
    rounds = "".join(f"<tr data-round='{r['number']}'><th scope='row'>第 {r['number']} 轮</th>"
                     f"<td>{'Agent' if r['kind'] == 'agent' else '人工'}</td><td>{r['version']}</td>"
                     f"<td>{text(r['submitted_by'])}</td><td>{time_tag(r['submitted_at'])}</td>"
                     f"<td data-field='pass-rate'>{ratio(r['pass_rate'])}</td>"
                     f"<td><a href='{text(api + '/rounds/' + str(r['number']) + '/export')}'>导出 ZIP</a></td></tr>" for r in data["rounds"])
    findings = "".join(f"<tr><td><a href='{text(base + '/s/' + quote(item['scenario'], safe=''))}'>{text(item['scenario'])}</a></td>"
                       f"<td><a href='{text(item['defect_url'])}'>{text(item['defect_url'])}</a></td>"
                       f"<td>{text(item['agent_executor'])}</td><td>{text(item['marker'])}</td></tr>"
                       for item in data["misjudgments"])
    content = (f"<nav><a href='/'>全部项目</a></nav>{batch_nav(project, data['name'], 'overview')}<h1>批次 {text(data['name'])}</h1>"
               f"<p>场景版本 {data['version']} · 当前场景 {data['scenario_count']} 条</p>"
               f"<p id='agent-state'>{agent_state(data)}</p>{report_link}"
               f"<p>场景文件推送时间：{time_tag(data['pushed_at'], 'pushed-at')}</p>"
               f"<dl class='metrics'>{metrics}</dl><div class='queues'>{''.join(queues)}</div>"
               "<section><h2>轮次记录</h2><div class='table-scroll'><table><thead><tr><th>轮次</th><th>种类</th>"
               "<th>场景版本</th><th>提交人</th><th>提交时间</th><th>每轮通过率</th><th>导出</th></tr></thead>"
               f"<tbody>{rounds}</tbody></table></div></section>"
               f"<section id='misjudgments'><h2>误判明细 <span id='misjudgment-count'>{len(data['misjudgments'])}</span></h2>"
               "<div class='table-scroll'><table><thead><tr><th>场景</th><th>缺陷链接</th><th>Agent 执行者</th><th>标记人</th></tr></thead>"
               f"<tbody>{findings}</tbody></table></div></section>")
    return layout("批次 " + data["name"], content, watch=(project, data["name"], data["etag"]))


STATUS = {"pass": "通过", "fail": "失败", "blocked": "受阻", "skip": "跳过"}
MANUAL_STATUS = {**STATUS, "fail": "不通过（失败）", "blocked": "不通过（受阻）"}


def evidence_preview(evidence):
    url = "/api/v1/evidence/" + evidence["sha256"]
    download = f"<a href='{text(url)}' download='{text(evidence['name'])}'>下载 {text(evidence['name'])}</a>"
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


def scenario(data, jira_url=""):
    base = batch_url(data["project"], data["batch"])
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
        results = []
        for result in group["results"]:
            kind = "Agent" if result["kind"] == "agent" else "人工"
            extras = []
            for field, name in [("finding", "问题或缺陷"), ("note", "备注"), ("session", "会话")]:
                if result.get(field):
                    value = text(result[field])
                    if (field == "finding" and result["kind"] == "manual" and jira_url
                            and re.fullmatch(re.escape(jira_url) + r"/browse/" + ISSUE_KEY + r"(?:[?#].*)?", result[field])):
                        value = f"<a href='{value}'>{value}</a>"
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
              f"{scenario_summary(summary)}{review_controls(data['project'], data['batch'], data)}</article>" if data["current"] else "")
    body = (f"<nav><a href='{text(base)}'>返回批次总览</a></nav>{batch_nav(data['project'], data['batch'], 'scenarios')}<h1>{text(data['scenario'])} · {text(data['title'])}</h1>"
            f"{removed}<p>节点：{text(data['node'])} · {'正向' if data['polarity'] == 'positive' else '反向'} · 优先级：{text(data['priority'])}</p>"
            f"<p>验证入口：{text(data['verify'])}</p>"
            f"<p>场景文件最新推送时间：{time_tag(data['pushed_at'])}</p>{identity_bar() if data['current'] else ''}{review}{''.join(sections)}")
    return layout(data["scenario"], body, jira_url, watch=(data["project"], data["batch"], data["etag"]))


def identity_bar():
    return ("<section id='review-identity'><h2>复核身份</h2><fieldset><legend>标记人与令牌（可随时更换）</legend>"
            "<label>标记人<input name='marker' autocomplete='off'></label>"
            "<label>写入令牌<input name='token' type='password' autocomplete='off'></label>"
            "<label>开发者令牌<input name='admin-token' type='password' autocomplete='off'></label>"
            "<button type='button' id='save-identity'>保存标记人与令牌</button></fieldset>"
            "<p id='identity-message' role='status' aria-live='polite'></p></section>")


def review_controls(project, name, item):
    action = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(name, safe='')}/marks"
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


def batch_nav(project, name, current):
    base = batch_url(project, name)
    return "<nav aria-label='批次导航'>" + "".join(
        f"<a href='{text(base + suffix)}'" + (" aria-current='page'" if key == current else "") + f">{label}</a>"
        for key, suffix, label in [("overview", "", "总览"), ("scenarios", "/scenarios", "SOP 场景"),
                                   ("cases", "/cases", "用例"), ("progress", "/progress", "进度"), ("merges", "/merges", "合并"),
                                   ("sop", "/sop", "SOP")]) + "</nav>"


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


def sop(project, name, sources, query=None):
    query = query or {}
    content = batch_nav(project, name, 'sop') + f'<h1>批次 {text(name)} · SOP</h1>'
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
            content += (f'<form method="get" action="{text(batch_url(project, name) + "/sop")}">' +
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


def progress(project, data, coverage):
    def metric(key, label, value):
        return f"<div id='metric-{key}'><dt>{label}</dt><dd>{value}</dd></div>"

    development = metric("pending", "待执行", data["queue_counts"]["pending"])
    for key, label in [("executed_progress", "已执行进度"), ("agent_pass_rate", "Agent 验收通过率"), ("first_pass_rate", "一次通过率")]:
        development += metric(key, label, ratio(data["statistics"][key]))
    testing = metric("retest", "待复核", data["queue_counts"]["retest"])
    for key, label in [("reviewed_progress", "已复核进度"), ("manual_pass_rate", "人工复测通过率")]:
        testing += metric(key, label, ratio(data["statistics"][key]))
    testing += metric("misjudgments", "误判", len(data["misjudgments"]))
    content = (batch_nav(project, data["name"], "progress") + f"<h1>批次 {text(data['name'])} · 进度</h1>"
               f"<div class='progress-columns'><section><h2>开发侧</h2><dl>{development}</dl></section>"
               f"<section><h2>测试侧</h2><dl>{testing}</dl></section></div>"
               f"<dl>{metric('coverage', '用例覆盖', ratio(coverage))}</dl>")
    return layout("进度 · " + data["name"], content, watch=(project, data["name"], data["etag"]))


def cases(project, name, data, scenarios):
    base = batch_url(project, name)
    api = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(name, safe='')}"
    content = (batch_nav(project, name, "cases") + f"<h1>批次 {text(name)} · 用例</h1>" + identity_bar() +
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
    current = {s["scenario"] for s in scenarios}
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
            content += (f"<a href='{text(base + '/s/' + quote(item['id'], safe=''))}'>已有对应场景</a>"
                        if item["id"] in current else "<p>尚无对应场景</p>")
            content += "</article>"
        content += "</section>"
    if not groups:
        content += "<p>尚无用例，请选择文件上传。</p>"
    return layout("用例 · " + name, content, watch=(project, name, data["etag"]))


def scenario_fields(body):
    parts = []
    for key, label in [("initial_state", "初始状态"), ("steps", "操作步骤"), ("expected", "预期结果"),
                       ("forbidden_effects", "禁止产生的影响"), ("refs", "来源引用")]:
        values = "".join(f"<li>{text(v)}</li>" for v in body.get(key, []))
        parts.append(f"<h3>{label}</h3><ul>{values}</ul>" if values else f"<h3>{label}</h3><p>未声明</p>")
    regression = body.get("regression")
    parts.append("<h3>回归声明</h3><pre>" + text(json.dumps(regression, ensure_ascii=False, indent=2)) + "</pre>"
                 if regression else "<h3>回归声明</h3><p>未声明</p>")
    return "".join(parts)


def latest_result(result, kind):
    if not result:
        return ""
    link = (f"<a href='{text(result['finding'])}'>{text(result['finding'])}</a>" if kind == "人工" and result.get("finding") else "")
    note = f"<p>备注：{text(result['note'])}</p>" if result.get("note") else ""
    label = (MANUAL_STATUS if kind == "人工" else STATUS)[result["status"]]
    return (f"<p>{kind} · {label} · {'标记人' if kind == '人工' else '执行者'}：{text(result['executor'])} · "
            f"{time_tag(result['executed_at'])}</p>{link}{note}")


def scenario_summary(item):
    return (f"<div data-latest-agent=''>{latest_result(item['latest_agent'], 'Agent')}</div>"
            f"<div data-latest-manual=''>{latest_result(item['latest_manual'], '人工')}</div>"
            f"<p data-queue=''>所在队列：{QUEUE[item['queue']]}</p>")


def scenarios(project, name, data, jira_url=""):
    base = batch_url(project, name)
    by_id = {item["scenario"]: item for item in data["scenarios"]}
    content = batch_nav(project, name, "scenarios") + f"<h1>批次 {text(name)} · SOP 场景</h1>"
    if not data["has_version"]:
        content += f"<p>尚无场景，测试岗上传用例后由合并生成</p><a href='{text(base + '/cases')}'>用例页</a>"
        return layout("SOP 场景 · " + name, content, watch=(project, name, data["etag"]))
    content += identity_bar()
    content += "<nav aria-label='场景筛选'>" + "".join(
        f"<a href='?filter={key}' data-filter='{key}'>{label} <span>{data['filters'][key]}</span></a>"
        for key, label in FILTERS) + "</nav>"
    for node in data["nodes"]:
        content += (f"<section class='scenario-node' data-node='{text(node['id'])}'><h2>{text(node['id'])} · {text(node['title'])}</h2>"
                    f"<p>角色：{text(node['actor'])} · 入口：{text(node['entry'])}</p>")
        for source in node["scenarios"]:
            item = by_id[source["id"]]
            regression = {'passed': '检查通过', 'failed': '检查失败', 'pending': '检查待运行'}.get(item.get('regression_status'), '')
            content += (f"<article class='scenario-card' data-scenario='{text(source['id'])}' "
                        f"data-status='{text(item['status'] or '')}' data-agent-pass='{str(bool(item['latest_agent'] and item['latest_agent']['status'] == 'pass')).lower()}' "
                        f"data-unreviewed='{str(item['latest_manual'] is None).lower()}'>"
                        f"<h3><a href='{text(base + '/s/' + quote(source['id'], safe=''))}'>{text(source['id'])}</a> · {text(source['title'])}</h3>"
                        f"<p>{'正向' if source['polarity'] == 'positive' else '反向'} · 优先级：{text(source['priority'])} · 角色：{text(source['actor'])} · 验证入口：{text(source['verify'])}</p>"
                        f"<p>{regression}</p>{scenario_fields(source)}{scenario_summary(item)}{review_controls(project, name, item)}</article>")
        content += "</section>"
    return layout("SOP 场景 · " + name, content, jira_url, watch=(project, name, data["etag"]))


def merges(project, name, tasks, selected=None):
    api = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(name, safe='')}"
    content = (batch_nav(project, name, 'merges') + f"<h1>批次 {text(name)} 的任务</h1>" + identity_bar() +
               "<p>未授权项目保存草稿；授权项目核对磁盘版本后写入工作区。待同步时请核对工作区的修改。</p>" +
               f"<button type='button' data-developer-action='{text(api + '/merges')}'>重新合并</button>" +
               "<p id='developer-message' role='status' aria-live='polite'></p>")
    current = next((t for t in tasks if str(t['id']) == selected), tasks[-1] if tasks else None)
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
            content += f"<li>{text(pair['case'])} → {text('、'.join(pair['scenarios']))}</li>"
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
        if task['status'] in ('失败', '已中断', 'Codex 不可用', '已作废'):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/retry'>重试</button>"
        if task['status'] in ('排队中', '运行中', '已中断', '待同步', '待补充', '待提交', '等待浏览器'):
            content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/cancel'>放弃</button>"
        content += '</section>'
    return layout('任务', content)


def fixes(project, tasks, selected=None):
    content = ("<h1>修复任务</h1>" + identity_bar() +
               "<p id='developer-message' role='status' aria-live='polite'></p>")
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
            f"{text(problem['handling'])} · {text(problem['reason'])} · {text(problem['plan'])}" +
            f"<p>{text(problem['input'].get('jira_error', ''))}</p></li>")
    content += '</ul><h3>尝试记录</h3>'
    for attempt in task['attempts']:
        content += (f"<section><h4>第 {attempt['number']} 次 · {text(attempt['outcome'])}</h4><p>{text(attempt['reason'])}</p>" +
            f"<p>提交：{text(attempt['commit_id'])}</p><p>{text(attempt['result'].get('root_cause', ''))}</p>" +
            '<pre>' + text(json.dumps(attempt['validation'], ensure_ascii=False, indent=2)) + '</pre></section>')
    content += "<label>开发者说明<textarea id='developer-note'></textarea></label>"
    if task['status'] == '待开发者处理':
        content += f"<button type='button' data-developer-note='' data-developer-action='/api/v1/tasks/{task['id']}/approve'>批准方案</button>"
    if task['status'] in ('已中断', 'Codex 不可用') or task.get('retry_reason') in ('detached', 'interrupted'):
        content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/retry'>重试</button>"
    else:
        content += "<button type='button' disabled>重试</button>"
    if task['status'] not in ('已合入', '已放弃', '待合入'):
        content += f"<button type='button' data-developer-action='/api/v1/tasks/{task['id']}/cancel'>放弃</button>"
    for name, url in task['files'].items():
        content += f"<p><a href='{text(url)}' download>{text(name)}</a></p>"
    return layout('修复任务', content)
