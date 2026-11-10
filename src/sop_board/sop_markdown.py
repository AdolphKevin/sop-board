"""只生成允许的 Markdown 元素；原文的所有普通文字先转义。

有意只支持 SOP 页面约定的子集，不解释原生 HTML、图片或其他嵌入内容。
段落内的换行按原文保留（SOP 与待确认问题里“**场景**”这类小标题后面紧跟正文）；
下划线只在词边界处表示强调，TC_PLAN_002 这类编号保持原样。
"""

import hashlib
from html import escape
import re
from urllib.parse import urlsplit


HEADING = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)\s*$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^`]*)$')
LIST = re.compile(r'^\s*([-+*]|\d+[.)])\s+(.+)$')


def link_end(raw, start):
    """链接地址可含括号，例如 javascript:alert(1) 也须整体作为文字保留。"""
    depth = 1
    for index in range(start, len(raw)):
        if raw[index] == '(':
            depth += 1
        elif raw[index] == ')':
            depth -= 1
            if depth == 0:
                return index
    return None


def inline(raw, depth=0):
    """返回安全 HTML 与可见文字，标题匹配使用同一份可见文字。"""
    if depth > 20:
        return escape(raw), raw
    html, plain = [], []
    index = 0
    while index < len(raw):
        if raw[index] == '\\' and index + 1 < len(raw) and raw[index + 1] in r'\`*_{}[]()#+-.!|':
            html.append(escape(raw[index + 1]))
            plain.append(raw[index + 1])
            index += 2
            continue
        if raw[index] == '`':
            marker = re.match(r'`+', raw[index:])[0]
            end = raw.find(marker, index + len(marker))
            if end >= 0:
                value = raw[index + len(marker):end]
                html.append('<code>' + escape(value) + '</code>')
                plain.append(value)
                index = end + len(marker)
                continue
        if raw[index] == '[':
            label_end = raw.find('](', index + 1)
            end = link_end(raw, label_end + 2) if label_end >= 0 else None
            if end is not None:
                literal = raw[index:end + 1]
                url = raw[label_end + 2:end].strip()
                try:
                    parsed = urlsplit(url)
                    allowed = parsed.scheme.lower() in ('http', 'https') and bool(parsed.netloc)
                except ValueError:
                    allowed = False
                if allowed and not any(ord(c) < 32 for c in url):
                    # 链接内不再解释链接，避免嵌套 a 元素。
                    label = raw[index + 1:label_end]
                    html.append(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>')
                    plain.append(label)
                else:
                    html.append(escape(literal))
                    plain.append(literal)
                index = end + 1
                continue
        marker = next((m for m in ('**', '__', '*', '_') if raw.startswith(m, index)), None)
        if marker and marker[0] == '_' and index > 0 and _word(raw[index - 1]):
            marker = None
        if marker:
            end = raw.find(marker, index + len(marker))
            while marker[0] == '_' and end >= 0 and end + len(marker) < len(raw) and _word(raw[end + len(marker)]):
                end = raw.find(marker, end + 1)
            if end > index + len(marker):
                rendered, label = inline(raw[index + len(marker):end], depth + 1)
                tag = 'strong' if len(marker) == 2 else 'em'
                html.append(f'<{tag}>{rendered}</{tag}>')
                plain.append(label)
                index = end + len(marker)
                continue
        html.append(escape(raw[index]))
        plain.append(raw[index])
        index += 1
    return ''.join(html), ''.join(plain)


def _word(char):
    return char.isalnum() or char == '_'


def cells(line):
    return re.split(r'(?<!\\)\|', line.strip().strip('|'))


def table_start(lines, index):
    return (index + 1 < len(lines) and '|' in lines[index] and
            all(re.fullmatch(r'\s*:?-{3,}:?\s*', c) for c in cells(lines[index + 1])) and
            len(cells(lines[index])) == len(cells(lines[index + 1])))


def render(raw, path, used=None):
    """返回 (HTML, 标题列表)。used 为同一文档已用的锚点，分段渲染时传入以保持锚点唯一。"""
    lines = raw.splitlines()
    output, headings = [], []
    used = set() if used is None else used
    prefix = 'sop-' + hashlib.sha256(path.encode()).hexdigest()[:12] + '-'
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = FENCE.match(line)
        if fence:
            marker, language = fence.groups()
            index += 1
            code = []
            while index < len(lines) and not re.fullmatch(r' {0,3}' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*', lines[index]):
                code.append(lines[index])
                index += 1
            value = '\n'.join(code) + ('\n' if code else '')
            if language.strip().lower() == 'mermaid':
                output.append('<pre class="mermaid">' + escape(value) + '</pre>')
            else:
                output.append('<pre><code>' + escape(value) + '</code></pre>')
            index += 1
            continue
        heading = HEADING.match(line)
        if heading:
            rendered, title = inline(re.sub(r'[ \t]+#+[ \t]*$', '', heading[2]))
            slug = re.sub(r'[^\w-]+', '-', title.lower()).strip('-') or 'heading'
            anchor = prefix + slug
            suffix = 2
            while anchor in used:
                anchor = prefix + slug + f'-{suffix}'
                suffix += 1
            used.add(anchor)
            headings.append(dict(title=title, anchor=anchor, level=len(heading[1])))
            level = len(heading[1])
            output.append(f'<h{level} id="{escape(anchor)}">{rendered}</h{level}>')
            index += 1
            continue
        if table_start(lines, index):
            headers = cells(line)
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip() and '|' in lines[index]:
                row = cells(lines[index])
                rows.append('<tr>' + ''.join('<td>' + inline(c.strip())[0] + '</td>' for c in row) + '</tr>')
                index += 1
            output.append('<div class="table-scroll"><table><thead><tr>' +
                          ''.join('<th>' + inline(c.strip())[0] + '</th>' for c in headers) +
                          '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
            continue
        item = LIST.match(line)
        if item:
            ordered = item[1][0].isdigit()
            tag = 'ol' if ordered else 'ul'
            items = []
            while index < len(lines) and (item := LIST.match(lines[index])) and item[1][0].isdigit() == ordered:
                items.append('<li>' + inline(item[2])[0] + '</li>')
                index += 1
            output.append(f'<{tag}>' + ''.join(items) + f'</{tag}>')
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not (HEADING.match(lines[index]) or
                FENCE.match(lines[index]) or LIST.match(lines[index]) or table_start(lines, index)):
            paragraph.append(lines[index])
            index += 1
        output.append('<p>' + '<br>\n'.join(inline(part)[0] for part in paragraph) + '</p>')
    return '\n'.join(output), headings


ANSWER = re.compile(r'^\s*(?:\*\*答复\*\*|#{4,6}\s*答复)\s*[:：]?\s*(.*)$')
ANSWERER = re.compile(r'^\s*(?:\*\*答复人\*\*|#{4,6}\s*答复人)')
UNANSWERED = {'', '-', '—', '无', '待答复', '（待答复）', '(待答复)', '待填写', '（待填写）', 'TODO', 'TBD'}


def answer_status(lines):
    """待确认问题的一节：有“答复”小节时返回 answered / open，没有时返回 None。"""
    for index, line in enumerate(lines):
        found = ANSWER.match(line)
        if not found:
            continue
        text = [found[1]]
        for rest in lines[index + 1:]:
            if ANSWERER.match(rest) or HEADING.match(rest):
                break
            text.append(rest)
        value = '\n'.join(text).strip()
        return 'open' if value in UNANSWERED else 'answered'
    return None


def sections(raw, path):
    """按 1～3 级标题把文档切成节（代码块内的 # 不算标题），每节各自渲染。

    返回 [{anchor, level, title, html, answer}]；文档开头没有标题的文字归入 level 0 的一节。
    """
    lines = raw.splitlines()
    chunks, current, fence = [], [], None
    for line in lines:
        opening = FENCE.match(line)
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*', line):
                fence = None
        elif opening:
            fence = opening[1]
        elif (heading := HEADING.match(line)) and len(heading[1]) <= 3 and current:
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    used, result = set(), []
    for chunk in chunks:
        html, headings = render('\n'.join(chunk), path, used)
        first = HEADING.match(chunk[0])
        level = len(first[1]) if first and len(first[1]) <= 3 else 0
        title = headings[0]['title'] if level else ''
        anchor = headings[0]['anchor'] if level else 'sop-' + hashlib.sha256(path.encode()).hexdigest()[:12] + '-intro'
        if not html.strip():
            continue
        result.append(dict(anchor=anchor, level=level, title=title, html=html, answer=answer_status(chunk[1:] if level else chunk)))
    return result
