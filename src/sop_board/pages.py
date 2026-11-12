"""页面外壳：工作台与业务 SOP 由 board.js 渲染；运行记录、报告、待人处理等为服务端渲染的简单页面。

所有页面共用顶栏：项目、导航、署名（姓名填一次存浏览器 localStorage，各页共用）与访问范围。
表单里每个输入框各占一行。
"""
from html import escape
import json
from urllib.parse import quote

from .status import ORDER

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234f46e5'/%3E"
        "%3Cpath d='M9 11h14M9 16h14M9 21h9' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E")
NAV = (('', '工作台'), ('/sop', '业务 SOP'), ('/inbox', '收件箱与待答复'),
       ('/runs', '运行记录'), ('/attention', '待人处理'), ('/report', '报告'), ('/atlas', '交互地图'))


def layout(project, title, body, *, local, active='', page='', data=None, operator=None, wide=False, scripts=()):
    links = ''.join(f'<a href="/p/{quote(project)}{path}"{" class=active" if path == active else ""}>{escape(label)}</a>'
                    for path, label in NAV)
    config = dict(data or {}, project=project, local=local, page=page, operator=operator if local else None)
    payload = escape(json.dumps(config, ensure_ascii=False), quote=False)
    extra = ''.join(f'<script src="/assets/{name}"></script>' for name in scripts)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Flow Board</title><link rel="icon" href="{ICON}"><link rel="stylesheet" href="/assets/board.css"></head>
<body class="{'wide' if wide else 'plain'}">
<header class="top">
  <a class="brand" href="/p/{quote(project)}"><span class="logo" aria-hidden="true"></span>Flow Board</a>
  <span class="project">{escape(project)}</span>
  <nav class="tabs">{links}</nav>
  <div class="top-right"><div id="signer" class="signer"></div><span class="scope {'local' if local else 'lan'}">{'本机' if local else '内网'}</span></div>
</header>
<script id="board-config" type="application/json">{payload}</script>
<main>{body}</main>
{extra}<script src="/assets/board.js"></script>
</body></html>'''


def page_head(title, lead=''):
    return f'<div class="page-head"><h1>{escape(title)}</h1>{f"<p>{lead}</p>" if lead else ""}</div>'


def tree_page(kind, search, empty):
    """左栏树加右栏的两栏外壳。左栏可隐藏（隐藏后左侧留一条窄栏用来恢复）、可拖右边缘的手柄调宽，树可一键全部展开或收起；
    状态由 board.js 记在 localStorage。"""
    return f'''
<section class="workbench{kind}">
  <aside id="sidebar" class="sidebar">
    <div class="sidebar-resizer" role="separator" aria-orientation="vertical" aria-controls="sidebar" tabindex="0"
         aria-label="调整左栏宽度" title="拖动调整左栏宽度，双击恢复 360px"></div>
    <div class="sidebar-head">
      <div class="sidebar-top">
        <label class="field compact"><span>检出</span><select id="checkout"></select></label>
        <button type="button" class="icon-button" data-sidebar="hide" aria-controls="sidebar" title="隐藏左栏" aria-label="隐藏左栏">«</button>
      </div>
      <input id="search" class="search" type="search" placeholder="{search}">
      <div id="filters" class="filters"></div>
      <div class="tree-tools"><button type="button" id="tree-all" class="tree-all" aria-controls="tree" disabled>全部展开</button></div>
    </div>
    <div id="tree" class="tree" role="tree"></div>
  </aside>
  <div class="sidebar-rail"><button type="button" class="icon-button" data-sidebar="show" aria-controls="sidebar" title="显示左栏" aria-label="显示左栏">»</button></div>
  <article id="detail" class="detail"><div class="empty">{empty}</div></article>
</section>'''


def workbench(project, *, local, operator=None):
    body = tree_page('', '搜索场景编号或标题', '<p>从左侧选择一个场景</p><p class="muted">四要素、证据与复测都在这里</p>')
    return layout(project, '工作台', body, local=local, page='workbench', operator=operator, wide=True,
                  data=dict(states=list(ORDER)))


def sop(project, *, local, operator=None):
    body = tree_page(' docs', '搜索标题', '<p>从左侧选择一份文档或一个问题</p>')
    return layout(project, '业务 SOP', body, local=local, active='/sop', page='sop', operator=operator, wide=True,
                  scripts=('vendor/mermaid/mermaid.min.js',))


def runs(project, *, local, operator=None):
    body = f'''
<section class="page">
  {page_head('运行记录', '执行会话、复验与沉淀的运行；本机可查看无头会话日志。')}
  <div class="toolbar"><label class="field compact"><span>检出</span><select id="checkout"></select></label></div>
  <div class="card"><h2>无头会话启动</h2><div id="launches"></div></div>
  <div class="card"><h2>运行</h2><div id="runs"></div></div>
</section>'''
    return layout(project, '运行记录', body, local=local, active='/runs', page='runs', operator=operator)


def report(project, checkout, markdown, evidence, *, local, operator=None):
    links = ''.join(f'<li><a href="/evidence/{sha}/{quote(path.split("-", 1)[1])}">{escape(path)}</a></li>'
                    for path, sha in evidence)
    zip_hint = '<span class="muted">证据 zip 用命令导出：sop-board report --zip</span>' if local else ''
    body = f'''
<section class="page">
  {page_head('报告', f'检出 {escape(checkout)} 的场景状态、证据、独立性与人工复测。')}
  <div class="toolbar"><a class="button primary" href="?checkout={quote(checkout)}&download=1">下载 Markdown</a>{zip_hint}</div>
  <div class="card"><pre class="report">{escape(markdown)}</pre></div>
  <div class="card"><h2>所引证据</h2><ul class="links">{links or '<li class="muted">无</li>'}</ul></div>
</section>'''
    return layout(project, '报告', body, local=local, active='/report', page='report', operator=operator)


def attention(project, checkout, items, *, local, operator=None):
    rows = ''.join(f'<tr><td class="mono">#{item["id"]}</td><td><span class="chip">{escape(item["kind"])}</span></td>'
                   f'<td>{escape(item["text"])}</td><td class="mono">{escape("、".join(item["scenarios"]))}</td>'
                   f'<td>{escape(item["created_by"])}</td><td>{escape(item.get("machine") or "")}'
                   f'{escape(" · 检出 " + item["checkout"]) if item.get("checkout") else ""}</td></tr>' for item in items)
    hint = '处理后运行 sop-board attention resolve &lt;编号&gt;。' if local else '需要开发者处理的事项。'
    body = f'''
<section class="page">
  {page_head('待人处理', f'本产品各台机器登记的待人处理，每条写明产出机器。{hint}')}
  <div class="card flush"><table><thead><tr><th>编号</th><th>种类</th><th>内容</th><th>场景</th><th>登记人</th><th>产出机器</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=6 class="muted center">没有待处理事项</td></tr>'}</tbody></table></div>
</section>'''
    return layout(project, '待人处理', body, local=local, active='/attention', page='attention', operator=operator)


def simple(project, title, active, body, *, local, operator=None):
    return layout(project, title, f'<section class="page">{page_head(title)}<div class="card">{body}</div></section>',
                  local=local, active=active, operator=operator)


STATUS = {'pending': '待合并', 'merged': '已合并', 'rejected': '已拒绝', 'open': '待处理', 'adopted': '已采纳'}


def signer_line():
    return '<p class="signed-as" data-signer></p>'


def inbox(project, items, questions, *, local, operator=None):
    rows = ''.join(f'<tr><td class="mono">#{i["id"]}</td><td>{"测试用例" if i["kind"] == "cases" else "业务 SOP"}</td>'
                   f'<td>{escape(i["filename"])}</td><td>{escape(i["uploaded_by"])}</td>'
                   f'<td><span class="chip">{STATUS.get(i["status"], i["status"])}</span></td></tr>' for i in items)
    cards = []
    answered = [q for q in questions if q['answer']]
    for q in [q for q in questions if not q['answer']]:
        if q['answer']:
            answer = (f'<div class="answer-done"><span class="pill pass">已答复</span><p>{escape(q["answer"])}</p>'
                      f'<p class="muted">答复人 {escape(q["answered_by"])}</p></div>')
        else:
            answer = (f'<form class="answer stack" data-question="{q["id"]}">'
                      f'<label class="field"><span>答复</span><textarea name="answer" rows="3" required></textarea></label>'
                      f'{signer_line()}<div class="form-actions"><button class="primary" type="submit">提交答复</button>'
                      f'<span class="message"></span></div></form>')
        pending = '' if q['answer'] else '<span class="pill retest">待答复</span>'
        cards.append(f'<article class="question"><header><span class="mono">#{q["id"]}</span>'
                     f'<span class="chip">关于 {escape(q["about"])}</span>{pending}'
                     f'<span class="muted">提问人 {escape(q["asked_by"])}</span></header>'
                     f'<p class="question-text">{escape(q["text"])}</p>{answer}</article>')
    body = f'''
<section class="page">
  {page_head('收件箱与待答复', '上传测试用例或业务 SOP，由开发者的会话合并进场景文件；会话提出的问题在这里答复。')}
  <div class="card"><h2>上传</h2>
  <form id="upload" class="stack">
    <label class="field"><span>类型</span><select name="kind"><option value="cases">测试用例（YAML）</option><option value="sop">业务 SOP（Markdown）</option></select></label>
    <label class="field"><span>文件</span><input type="file" name="file" required></label>
    {signer_line()}
    <div class="form-actions"><button class="primary" type="submit">上传</button><span class="message"></span></div>
  </form>
  <div id="upload-problems" class="problems"></div></div>
  <div class="card flush"><h2 class="pad">收件箱</h2>
  <table><thead><tr><th>编号</th><th>类型</th><th>文件</th><th>上传人</th><th>状态</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=5 class="muted center">收件箱为空</td></tr>'}</tbody></table></div>
  <div class="card"><h2>待答复</h2><div class="questions">{''.join(cards) or '<p class="muted">没有待答复的问题</p>'}</div>
  {f'<p class="muted">已答复 {len(answered)} 条，由开发者的会话读取。</p>' if answered else ''}</div>
</section>'''
    return layout(project, '收件箱与待答复', body, local=local, active='/inbox', page='inbox', operator=operator)


def atlas(project, checkout, entries, *, local, operator=None):
    blocks = []
    for item in entries:
        if item.get('problem'):
            blocks.append(f'<article class="card"><h2>{escape(item["entry"])}</h2><p class="finding">{escape(item["problem"])}</p></article>')
            continue
        if not item['exists']:
            blocks.append(f'<article class="card"><h2>{escape(item["entry"])}</h2><p class="muted">还没有交互地图</p></article>')
            continue
        stale = {s['page']: s['files'] for s in item['stale'] or []}
        rows = []
        for page in item['document']['pages']:
            controls = '、'.join(escape(next((t['text'] for t in c['name']), c['id'])) for c in page.get('controls', [])[:12])
            mark = f' <span class="pill retest">已过期：{escape("、".join(stale[page["id"]]))}</span>' if page['id'] in stale else ''
            title = ' / '.join(t['text'] for t in page.get('title', [])) or page['id']
            rows.append(f'<tr><td class="mono">{escape(page["id"])}</td><td>{escape(title)}{mark}</td>'
                        f'<td class="mono">{escape(page.get("route") or "")}</td><td>{controls}</td></tr>')
        blocks.append(f'<article class="card flush"><h2 class="pad">{escape(item["entry"])}</h2><table><thead><tr><th>页面</th><th>标题</th><th>路由</th><th>控件</th></tr></thead>'
                      f'<tbody>{"".join(rows)}</tbody></table></article>')
    body = f'''<section class="page">{page_head('交互地图', '地图只说明怎么操作，不提供预期。')}
{"".join(blocks) or '<div class="card"><p class="muted">没有声明 atlas 的 Web 入口</p></div>'}</section>'''
    return layout(project, '交互地图', body, local=local, active='/atlas', page='atlas', operator=operator)
