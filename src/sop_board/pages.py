"""页面外壳：工作台由 board.js 渲染；运行记录、报告、待人处理等为服务端渲染的简单页面。"""
from html import escape
import json
from urllib.parse import quote

NAV = (('', '工作台'), ('/runs', '运行记录'), ('/report', '报告'), ('/attention', '待人处理'),
       ('/inbox', '收件箱与待答复'), ('/drafts', '草稿批注'), ('/atlas', '交互地图'))


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
