"""把 Service 的读取结果呈现为服务端 HTML，不重新计算业务指标。"""

from html import escape
from urllib.parse import quote


def text(value):
    return escape(str(value), quote=True)


def batch_url(project, batch):
    return f"/p/{quote(project, safe='')}/b/{quote(batch, safe='')}"


def layout(title, content):
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{text(title)} · sop-board</title>"
            "<link rel='stylesheet' href='/assets/board.css'><script src='/assets/board.js' defer></script></head><body>"
            "<header><a href='/'>sop-board</a><span>SOP 验证协作看板</span></header>"
            f"<main>{content}</main></body></html>")


def home(data):
    projects = []
    for project in data["projects"]:
        batches = "".join(f"<li><a href='{text(batch_url(project['key'], batch['name']))}'>"
                          f"{text(batch['name'])}</a></li>" for batch in project["batches"])
        projects.append(f"<section><h2>{text(project['name'])}</h2><p>{text(project['id'])}</p>"
                        f"<ul>{batches}</ul></section>")
    return layout("项目与批次", "<h1>项目与批次</h1>" + ("".join(projects) or "<p>还没有项目，推送场景文件后会显示在这里。</p>"))


def ratio(value):
    return f"{value['numerator']}/{value['denominator']}" if value["denominator"] else "无数据"


def batch(project, data):
    base = batch_url(project, data["name"])
    api = f"/api/v1/projects/{quote(project, safe='')}/batches/{quote(data['name'], safe='')}"
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
                     f"<td>{text(r['submitted_by'])}</td><td>{text(r['submitted_at'])}</td>"
                     f"<td data-field='pass-rate'>{ratio(r['pass_rate'])}</td>"
                     f"<td><a href='{text(api + '/rounds/' + str(r['number']) + '/export')}'>导出 ZIP</a></td></tr>" for r in data["rounds"])
    findings = "".join(f"<tr><td><a href='{text(base + '/s/' + quote(item['scenario'], safe=''))}'>{text(item['scenario'])}</a></td>"
                       f"<td><a href='{text(item['defect_url'])}'>{text(item['defect_url'])}</a></td>"
                       f"<td>{text(item['agent_executor'])}</td><td>{text(item['marker'])}</td></tr>"
                       for item in data["misjudgments"])
    content = (f"<nav><a href='/'>全部项目</a></nav><h1>批次 {text(data['name'])}</h1>"
               f"<p>场景版本 {data['version']} · 当前场景 {data['scenario_count']} 条</p>"
               f"<dl class='metrics'>{metrics}</dl><div class='queues'>{''.join(queues)}</div>"
               "<section><h2>轮次记录</h2><div class='table-scroll'><table><thead><tr><th>轮次</th><th>种类</th>"
               "<th>场景版本</th><th>提交人</th><th>提交时间</th><th>每轮通过率</th><th>导出</th></tr></thead>"
               f"<tbody>{rounds}</tbody></table></div></section>"
               f"<section id='misjudgments'><h2>误判明细 <span id='misjudgment-count'>{len(data['misjudgments'])}</span></h2>"
               "<div class='table-scroll'><table><thead><tr><th>场景</th><th>缺陷链接</th><th>Agent 执行者</th><th>标记人</th></tr></thead>"
               f"<tbody>{findings}</tbody></table></div></section>")
    return layout("批次 " + data["name"], content)


STATUS = {"pass": "通过", "fail": "失败", "blocked": "受阻", "skip": "跳过"}


def evidence_preview(evidence):
    url = "/api/v1/evidence/" + evidence["sha256"]
    if evidence["content_type"].startswith("image/"):
        return f"<figure><img src='{text(url)}' alt='{text(evidence['name'])}' loading='lazy'><figcaption>{text(evidence['name'])}</figcaption></figure>"
    if "preview" in evidence:
        return f"<figure><figcaption>{text(evidence['name'])}（前 200 行）</figcaption><pre data-evidence='{text(evidence['sha256'])}'>{text(evidence['preview'])}</pre></figure>"
    return f"<p><a href='{text(url)}' download='{text(evidence['name'])}'>下载 {text(evidence['name'])}</a></p>"


def scenario(data):
    base = batch_url(data["project"], data["batch"])
    sections = []
    for group in data["groups"]:
        label = ("当前内容" if group["current"] else "移除前内容" if group["fingerprint"] == data["fingerprint"] else "场景已变更")
        fields = []
        for key, name in [("initial_state", "初始状态"), ("steps", "操作步骤"), ("expected", "预期结果"), ("forbidden_effects", "禁止产生的影响")]:
            values = "".join(f"<li>{text(v)}</li>" for v in group["body"][key])
            fields.append(f"<h3>{name}</h3><ul>{values}</ul>" if values else f"<h3>{name}</h3><p>未声明</p>")
        results = []
        for result in group["results"]:
            kind = "Agent" if result["kind"] == "agent" else "人工"
            extras = "".join(f"<p>{name}：{text(result[field])}</p>" for field, name in
                             [("finding", "问题或缺陷"), ("note", "备注"), ("session", "会话")]
                             if result.get(field))
            results.append(f"<article class='result'><h3>第 {result['number']} 轮 · {kind} · {STATUS[result['status']]}</h3>"
                           f"<p>执行者：{text(result['executor'])} · 执行时间：{text(result['executed_at'])}</p>"
                           f"<p>实际结果：{text(result['actual'])}</p>{extras}"
                           f"{''.join(evidence_preview(e) for e in result['evidence'])}</article>")
        sections.append(f"<section data-fingerprint='{text(group['fingerprint'])}'><h2>{label}</h2>"
                        f"<p class='fingerprint'>内容指纹：{text(group['fingerprint'])}</p>{''.join(fields)}"
                        f"{''.join(results) or '<p>尚无执行结果</p>'}</section>")
    removed = "<p class='notice'>已从场景文件移除</p>" if not data["current"] else ""
    body = (f"<nav><a href='{text(base)}'>返回批次总览</a></nav><h1>{text(data['scenario'])} · {text(data['title'])}</h1>"
            f"{removed}<p>节点：{text(data['node'])} · {'正向' if data['polarity'] == 'positive' else '反向'} · 优先级：{text(data['priority'])}</p>"
            f"<p>验证入口：{text(data['verify'])}</p>{mark_form(data)}{''.join(sections)}")
    return layout(data["scenario"], body)


def mark_form(data):
    if not data["current"]:
        return ""
    action = f"/api/v1/projects/{quote(data['project'], safe='')}/batches/{quote(data['batch'], safe='')}/marks"
    options = "".join(f"<option value='{value}'>{label}</option>" for value, label in STATUS.items())
    return (f"<section><h2>人工复核</h2><form id='mark-form' action='{text(action)}' method='post'>"
            f"<input type='hidden' name='scenario' value='{text(data['scenario'])}'>"
            f"<input type='hidden' name='fingerprint' value='{text(data['fingerprint'])}'>"
            "<fieldset><legend>标记人与令牌（可随时更换）</legend>"
            "<label>标记人<input name='marker' required autocomplete='off'></label>"
            "<label>写入令牌<input name='token' type='password' required autocomplete='off'></label>"
            "<button type='button' id='save-identity'>保存标记人与令牌</button></fieldset>"
            f"<label>复核状态<select name='status'>{options}</select></label>"
            "<label>实际结果<textarea name='actual' required rows='4'></textarea></label>"
            "<label>验证环境<input name='environment' required></label>"
            "<label>缺陷链接（失败时必填）<input name='defect_url' type='url'></label>"
            "<label>受阻或跳过的原因<textarea name='finding' rows='2'></textarea></label>"
            "<label>备注<textarea name='note' rows='2'></textarea></label>"
            "<button type='submit'>保存标记</button><p id='mark-message' role='status' aria-live='polite'></p>"
            "<noscript>请启用 JavaScript 后保存标记。</noscript></form></section>")
