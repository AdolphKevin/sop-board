"""页面外壳：工作台由 board.js 渲染；运行记录、报告、待人处理等为服务端渲染的简单页面。"""
from html import escape
import json
from urllib.parse import quote

NAV = (('', '工作台'), ('/runs', '运行记录'), ('/report', '报告'), ('/attention', '待人处理'),
       ('/inbox', '收件箱与待答复'), ('/drafts', '草稿批注'), ('/sop', '业务 SOP'), ('/atlas', '交互地图'))


def layout(project, title, body, *, local, active='', script=None, data=None):
    links = ''.join(f'<a href="/p/{quote(project)}{path}"{" class=active" if path == active else ""}>{escape(label)}</a>'
                    for path, label in NAV)
    scope = '本机' if local else '内网'
    config = f'<script id="board-config" type="application/json">{escape(json.dumps(data or {}, ensure_ascii=False), quote=False)}</script>'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Flow Board</title><link rel="stylesheet" href="/assets/board.css"></head>
<body data-local="{str(local).lower()}">
<header class="top"><strong>Flow Board · {escape(project)}</strong><nav>{links}</nav><span class="scope">{scope}</span></header>
{config}
<main>{body}</main>
{f'<script src="/assets/{script}"></script>' if script else ''}
</body></html>'''


def workbench(project, *, local):
    body = '''
<section class="workbench">
  <aside class="tree-pane">
    <div class="toolbar">
      <label>检出 <select id="checkout"></select></label>
      <input id="search" type="search" placeholder="搜索场景编号或标题">
    </div>
    <div id="filters" class="filters"></div>
    <div id="tree" class="tree"></div>
  </aside>
  <article id="detail" class="detail-pane"><p class="muted">从左侧选择一个场景。</p></article>
</section>'''
    return layout(project, '工作台', body, local=local, script='board.js', data=dict(project=project, local=local, page='workbench'))


def runs(project, *, local):
    body = '''
<section class="page">
  <div class="toolbar"><label>检出 <select id="checkout"></select></label></div>
  <h2>启动记录</h2><div id="launches"></div>
  <h2>运行</h2><div id="runs"></div>
</section>'''
    return layout(project, '运行记录', body, local=local, active='/runs', script='board.js',
                  data=dict(project=project, local=local, page='runs'))


def report(project, checkout, markdown, evidence, *, local):
    links = ''.join(f'<li><a href="/evidence/{sha}/{quote(path.split("-", 1)[1])}">{escape(path)}</a></li>'
                    for path, sha in evidence)
    body = f'''
<section class="page">
  <p><a class="button" href="?checkout={quote(checkout)}&download=1">下载 Markdown</a>
     {'<span class="muted">证据 zip 用命令导出：sop-board report --zip</span>' if local else ''}</p>
  <pre class="report">{escape(markdown)}</pre>
  <h2>所引证据</h2><ul>{links or '<li>无</li>'}</ul>
</section>'''
    return layout(project, '报告', body, local=local, active='/report')


def attention(project, checkout, items, *, local):
    rows = ''.join(f'<tr><td>#{item["id"]}</td><td>{escape(item["kind"])}</td><td>{escape(item["text"])}</td>'
                   f'<td>{escape("、".join(item["scenarios"]))}</td><td>{escape(item["created_by"])}</td></tr>' for item in items)
    hint = '<p class="muted">处理后在本机运行 sop-board attention resolve --id &lt;编号&gt;。</p>' if local else ''
    body = f'''
<section class="page">
  <p>检出 {escape(checkout)} 的待人处理事项。</p>{hint}
  <table><thead><tr><th>编号</th><th>种类</th><th>内容</th><th>场景</th><th>登记人</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=5>没有待处理事项</td></tr>'}</tbody></table>
</section>'''
    return layout(project, '待人处理', body, local=local, active='/attention')


def simple(project, title, active, body, *, local):
    return layout(project, title, f'<section class="page">{body}</section>', local=local, active=active)


STATUS = {'pending': '待合并', 'merged': '已合并', 'rejected': '已拒绝', 'open': '待处理', 'adopted': '已采纳'}


def inbox(project, items, questions, *, local):
    rows = ''.join(f'<tr><td>#{i["id"]}</td><td>{"用例" if i["kind"] == "cases" else "业务 SOP"}</td><td>{escape(i["filename"])}</td>'
                   f'<td>{escape(i["uploaded_by"])}</td><td>{STATUS.get(i["status"], i["status"])}</td></tr>' for i in items)
    question_rows = []
    for q in questions:
        if q['answer']:
            answered = f'<div>答复：{escape(q["answer"])}（{escape(q["answered_by"])}）</div>'
        else:
            answered = (f'<form class="answer" data-question="{q["id"]}"><input name="answer" placeholder="答复" required>'
                        f'<button>提交答复</button><span class="message"></span></form>')
        question_rows.append(f'<li><strong>#{q["id"]} 关于 {escape(q["about"])}</strong>：{escape(q["text"])}{answered}</li>')
    body = f'''
<section class="page">
  <h2>上传用例或业务 SOP</h2>
  <form id="upload" class="mark">
    <select name="kind"><option value="cases">测试用例（YAML）</option><option value="sop">业务 SOP（Markdown）</option></select>
    <input type="file" name="file" required>
    <button type="submit">上传</button><span class="message"></span>
  </form>
  <div id="upload-problems" class="finding"></div>
  <h2>收件箱（待合并）</h2>
  <table><thead><tr><th>编号</th><th>类型</th><th>文件</th><th>上传人</th><th>状态</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=5>没有待合并的条目</td></tr>'}</tbody></table>
  <h2>待答复</h2>
  <p class="muted">答复人姓名使用工作台复测时填写的姓名。</p>
  <ul class="questions">{''.join(question_rows) or '<li>没有问题</li>'}</ul>
</section>'''
    return layout(project, '收件箱与待答复', body, local=local, active='/inbox', script='board.js',
                  data=dict(project=project, local=local, page='inbox'))


def drafts(project, checkout, files, notes, *, local):
    blocks = []
    for item in files:
        own = [n for n in notes if n['file'] == item['name']]
        listed = ''.join(f'<li>#{n["id"]} [{escape(n["target"])}] {escape(n["text"])}（{escape(n["author"])}，{STATUS.get(n["status"], n["status"])}）</li>'
                         for n in own)
        targets = ''.join(f'<option value="{escape(s)}">{escape(s)}</option>' for node in item['nodes'] for s in node['scenarios'])
        blocks.append(f'''<article class="draft"><h2>{escape(item["name"])}</h2><p>{escape(item.get("requirement") or "")}</p>
  <p><a href="/p/{quote(project)}?checkout={quote(checkout)}">在工作台查看场景</a>（筛选“草稿”）</p>
  <ul>{listed or '<li>没有批注</li>'}</ul>
  <form class="annotate mark" data-file="{escape(item["name"])}"><select name="target"><option value="file">整个文件</option>{targets}</select>
  <input name="text" placeholder="批注内容" required><button>提交批注</button><span class="message"></span></form></article>''')
    body = f'<section class="page">{"".join(blocks) or "<p>没有草稿场景文件</p>"}</section>'
    return layout(project, '草稿批注', body, local=local, active='/drafts', script='board.js',
                  data=dict(project=project, local=local, page='drafts', checkout=checkout))


def sop(project, checkout, documents, *, local):
    blocks = ''.join(f'<article class="sop"><h2>{escape(name)}</h2>{html}</article>' for name, html in documents)
    body = f'<section class="page">{blocks or "<p>场景目录下没有业务 SOP（.md）</p>"}</section>'
    return layout(project, '业务 SOP', body, local=local, active='/sop').replace(
        '</body>', '<script src="/assets/vendor/mermaid/mermaid.min.js"></script><script src="/assets/sop.js"></script></body>')


def atlas(project, checkout, entries, *, local):
    blocks = []
    for item in entries:
        if item.get('problem'):
            blocks.append(f'<article><h2>{escape(item["entry"])}</h2><p class="finding">{escape(item["problem"])}</p></article>')
            continue
        if not item['exists']:
            blocks.append(f'<article><h2>{escape(item["entry"])}</h2><p class="muted">还没有交互地图</p></article>')
            continue
        stale = {s['page']: s['files'] for s in item['stale'] or []}
        rows = []
        for page in item['document']['pages']:
            controls = '、'.join(escape(next((t['text'] for t in c['name']), c['id'])) for c in page.get('controls', [])[:12])
            mark = f'<span class="badge old">已过期：{escape("、".join(stale[page["id"]]))}</span>' if page['id'] in stale else ''
            rows.append(f'<tr><td>{escape(page["id"])}</td><td>{escape(page["title"])}{mark}</td><td>{escape(page.get("route") or "")}</td><td>{controls}</td></tr>')
        blocks.append(f'<article><h2>{escape(item["entry"])}</h2><table><thead><tr><th>页面</th><th>标题</th><th>路由</th><th>控件</th></tr></thead>'
                      f'<tbody>{"".join(rows)}</tbody></table></article>')
    body = f'<section class="page"><p class="muted">地图只说明怎么操作，不提供预期。</p>{"".join(blocks) or "<p>没有声明 atlas 的 Web 入口</p>"}</section>'
    return layout(project, '交互地图', body, local=local, active='/atlas')
