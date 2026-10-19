"""SOP-35 页面：真实 HTTP 返回的结构、转义、版本与标题链接。"""

from datetime import datetime
from urllib.parse import urlencode
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest

from helpers import BASE, KEY, assert_error, demo, push, push_body
from html_helpers import page
from test_sop_sources import source


WEB = f'/p/{KEY}/b/demo'


def test_sop_empty_page_and_batch_navigation(board):
    """SOP-35-P01/P02/N02：已有批次无原文提示，所有批次导航可到达 SOP。"""
    assert_error(board.request('GET', WEB + '/sop', auth=False), 404, 'project-not-found')
    push(board)
    for suffix in ('', '/scenarios', '/cases', '/progress', '/merges', '/sop', '/s/N1-P01'):
        doc = page(board, WEB + suffix)
        nav = doc.one('nav', **{'aria-label': '批次导航'})
        link = nav.one('a', href=WEB + '/sop')
        assert link.text == 'SOP'
        if suffix == '/sop':
            assert link.attrs['aria-current'] == 'page'
            assert '尚无 SOP 原文' in doc.text
    assert_error(board.request('GET', WEB.replace('/demo', '/absent') + '/sop', auth=False),
                 404, 'batch-not-found')


MARKDOWN = '''# 登录 SOP

## 登录失败处理

第一段，使用 `retry()`，**加粗** 与 *强调*，还有 __粗体__ 和 _斜体_。
下一行仍在段落。

- 检查用户名
- 检查密码

1. 重试
2. 联系管理员

| 状态 | 处理 |
| --- | --- |
| 失败 | `retry()` |

```python
print("<script>code</script>")
```

[帮助](https://example.invalid/help?q=1&lang=zh) 与 [HTTP](http://example.invalid/guide)

<script>alert(1)</script>
<img src=x onerror=alert(2)>
[点我](javascript:alert(3)) [数据](data:text/html,hello) [本地](file:///etc/passwd)
'''


def test_sop_renders_markdown_subset_and_escapes_untrusted_html_and_links(board):
    """SOP-35-P01：各 Markdown 元素可读，原文 HTML 与非 HTTP 链接仅显示文字。"""
    push(board, push_body(sop_sources=[source(MARKDOWN)]))
    doc = page(board, WEB + '/sop')
    article = doc.one('article', **{'class': 'sop-content'})
    headings = [n for n in article.find_all() if n.tag in ('h1', 'h2')]
    assert [n.text for n in headings] == ['登录 SOP', '登录失败处理']
    assert all(n.attrs.get('id') for n in headings)
    assert [li.text for li in article.one('ul').find_all('li')] == ['检查用户名', '检查密码']
    assert [li.text for li in article.one('ol').find_all('li')] == ['重试', '联系管理员']
    assert [n.text for n in article.find_all('strong')] == ['加粗', '粗体']
    assert [n.text for n in article.find_all('em')] == ['强调', '斜体']
    assert article.one('table').one('tbody').text == '失败retry()'
    assert article.one('pre').one('code').text == 'print("<script>code</script>")\n'
    assert '下一行仍在段落。' in article.find_all('p')[0].text
    assert article.one('a', href='https://example.invalid/help?q=1&lang=zh').text == '帮助'
    assert article.one('a', href='http://example.invalid/guide').text == 'HTTP'
    assert not article.find_all('script') and not article.find_all('img')
    assert '<script>alert(1)</script>' in article.text
    assert '<img src=x onerror=alert(2)>' in article.text
    assert '[点我](javascript:alert(3))' in article.text
    assert len(article.find_all('a')) == 2
    html = board.request('GET', WEB + '/sop', auth=False)[1].decode()
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '&lt;img src=x onerror=alert(2)&gt;' in html
    # 用户主动打开的 HTTP 链接允许外部地址；所有自动加载资源只能同源。
    for element in doc.find_all():
        for attr in ('src', 'href'):
            value = element.attrs.get(attr)
            if value is not None:
                assert value.startswith(('/', '#', '?')) or (element.tag == 'a' and value.startswith(('http://', 'https://')))
                assert not value.startswith('//')


def test_mermaid_uses_only_bundled_script_and_escaped_container(board):
    """SOP-35-P01：图源转义后交给本地 Mermaid；所有 src/href 均在看板地址内。"""
    graph = 'flowchart LR\n  A[登录] --> B[完成]\n'
    push(board, push_body(sop_sources=[source('# 图示\n\n```mermaid\n' + graph + '```\n')]))
    doc = page(board, WEB + '/sop')
    assert doc.one('pre', **{'class': 'mermaid'}).text == graph
    resources = []
    for element in doc.find_all():
        for attr in ('src', 'href'):
            if attr in element.attrs:
                value = element.attrs[attr]
                assert value.startswith(('/', '?', '#')) and not value.startswith('//')
                if element.tag in ('script', 'link'):
                    resources.append(value)
                    status, raw, _ = board.request('GET', value, auth=False)
                    assert status == 200 and raw
    assert '/assets/vendor/mermaid/mermaid.min.js' in resources
    assert '/assets/sop.js' in resources
    from conftest import ROOT
    vendor = ROOT / 'src/sop_board/assets/vendor/mermaid'
    assert (vendor / 'LICENSE').is_file()
    assert board.request('GET', '/assets/vendor/mermaid/mermaid.min.js', auth=False)[1] == (vendor / 'mermaid.min.js').read_bytes()
    # 图源也不能越过元素边界；图内声明的外部资源由响应策略限制。
    push(board, push_body(sop_sources=[source('```mermaid\n</pre><script>alert(4)</script>\n```\n')]))
    doc = page(board, WEB + '/sop')
    assert doc.one('pre', **{'class': 'mermaid'}).text == '</pre><script>alert(4)</script>\n'
    assert all(s.attrs.get('src', '').startswith('/assets/') for s in doc.find_all('script'))
    policy = board.request('GET', WEB + '/sop', auth=False)[2]['Content-Security-Policy']
    assert "default-src 'self'" in policy and "script-src 'self'" in policy


def test_sop_version_metadata_and_line_diff_are_scoped_to_one_path(board_factory):
    """SOP-35-P02：最新原文、两版元数据、本地时间与逐行增删；不能跨原文比较。"""
    board = board_factory(env={'TZ': 'Asia/Kathmandu'})
    first = source('# 登录\n保留行\n旧内容 <script>old()</script>\n结尾\n')
    body, _ = push(board, push_body(sop_sources=[first, source('# 另一份\n', path='docs/other.md')]))
    second = source('# 登录\n保留行\n新内容 <script>new()</script>\n新增一行\n结尾\n',
                    commit='sop-c2', confirmed_by='owner-b')
    push(board, dict(body, sop_sources=[second]))
    push(board, dict(body, sop_sources=[second]))
    doc = page(board, WEB + '/sop')
    section = doc.one('section', **{'data-sop-path': first['path']})
    article = section.one('article', **{'class': 'sop-content'})
    assert '新内容' in article.text and '旧内容' not in article.text
    rows = section.one('table', **{'class': 'sop-versions'}).one('tbody').find_all('tr')
    assert [row.attrs['data-version'] for row in rows] == ['2', '1']
    for row, original in zip(rows, [second, first]):
        assert original['commit'] in row.text
        assert original['sha256'][:12] in row.text and original['confirmed_by'] in row.text
        stamp = row.one('time')
        recorded = board.sql('SELECT recorded_at FROM sop_versions WHERE path=? AND version=?',
                             (first['path'], int(row.attrs['data-version'])))[0][0]
        local = datetime.fromisoformat(recorded).astimezone(ZoneInfo('Asia/Kathmandu'))
        assert stamp.text == local.strftime('%Y-%m-%d %H:%M:%S') + ' +05:45'
    form = section.one('form')
    assert form.attrs['method'].lower() == 'get'
    assert form.one('input', name='path').attrs['value'] == first['path']
    assert [s.attrs['name'] for s in form.find_all('select')] == ['from', 'to']
    query = urlencode(dict(path=first['path'], **{'from': 1, 'to': 2}))
    diff = page(board, WEB + '/sop?' + query).one('table', **{'class': 'sop-diff'})
    rows = diff.one('tbody').find_all('tr')
    assert [(r.attrs['data-kind'], r.one('code').text) for r in rows] == [
        ('context', '# 登录'), ('context', '保留行'), ('delete', '旧内容 <script>old()</script>'),
        ('insert', '新内容 <script>new()</script>'), ('insert', '新增一行'), ('context', '结尾')]
    assert not diff.find_all('script')
    assert [[c.text for c in r.find_all('td')[:2]] for r in rows] == [
        ['1', '1'], ['2', '2'], ['3', ''], ['', '3'], ['', '4'], ['4', '5']]
    invalid = urlencode(dict(path='docs/other.md', **{'from': 1, 'to': 2}))
    assert not page(board, WEB + '/sop?' + invalid).find_all('table', **{'class': 'sop-diff'})


def test_scenario_refs_link_only_exact_visible_headings_with_unique_anchors(board):
    """SOP-35-P02：refs 按标题可见文字精确匹配，重名标题/多份原文锚点不冲突。"""
    document = demo()
    document['sops'][0]['scenarios'][0]['refs'] = ['登录失败处理', '登录流程', 'TC_DEMO_LOGIN_001']
    content = '# 登录 SOP\n\n## **登录失败处理**\n\n## 登录失败处理\n\n## 登录失败处理-2\n'
    body, _ = push(board, push_body(document, sop_sources=[source(content), source(content, path='docs/other.md')]))
    sop = page(board, WEB + '/sop')
    headings = [h for a in sop.find_all('article', **{'class': 'sop-content'})
                for h in a.find_all() if h.tag in ('h1', 'h2')]
    anchors = [h.attrs['id'] for h in headings]
    assert len(anchors) == len(set(anchors))
    detail = page(board, WEB + '/s/N1-P01')
    link = next(a for a in detail.find_all('a') if a.text == '登录失败处理')
    url, anchor = link.attrs['href'].split('#')
    assert url == WEB + '/sop'
    assert sop.one(id=unquote(anchor)).text == '登录失败处理'
    for label in ('登录流程', 'TC_DEMO_LOGIN_001'):
        assert label in detail.text and not [a for a in detail.find_all('a') if a.text == label]
    # 仅改原文也改变详情的刷新标识，使已打开的详情更新标题链接。
    old_state = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    push(board, dict(body, sop_sources=[source('# 已删除原标题\n'), source('# 另一份\n', path='docs/other.md')]))
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] != old_state
    assert not [a for a in page(board, WEB + '/s/N1-P01').find_all('a') if a.text == '登录失败处理']


@pytest.mark.parametrize('heading,title', [('# C#', 'C#'), ('## 登录失败处理 ###', '登录失败处理')])
def test_heading_preserves_literal_hash_and_ignores_fenced_titles(board, heading, title):
    """SOP-35-P01/P02：标题末尾的井号保留，只有空格后的井号是结束标记，代码内不建锚点。"""
    document = demo()
    document['sops'][0]['scenarios'][0]['refs'] = [title, '代码里的标题']
    push(board, push_body(document, sop_sources=[source(heading + '\n\n```text\n# 代码里的标题\n```\n')]))
    article = page(board, WEB + '/sop').one('article', **{'class': 'sop-content'})
    headings = [h for h in article.find_all() if h.tag in ('h1', 'h2')]
    assert [h.text for h in headings] == [title]
    detail = page(board, WEB + '/s/N1-P01')
    assert len([a for a in detail.find_all('a') if a.text == title]) == 1
    assert not [a for a in detail.find_all('a') if a.text == '代码里的标题']
