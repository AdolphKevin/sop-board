"""运行中的临时改动（已定决策 31 第三段）：执行会话为准备起点改了测试对象，就登记改了什么、怎样恢复，结束前恢复。

看板不理解被改的对象，只保管这份清单：提交时列出没有恢复的，并登记待人处理。
哪些对象可以临时改，由团队写在产品备注里。
"""
from pathlib import Path

from . import progress
from .errors import reject
from .store import now

FILE = 'changes.json'
LIMIT = 500


def read(directory):
    return progress.read_json(Path(directory) / FILE, [])


def text_of(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > LIMIT:
        reject(422, 'invalid-arguments', field, f'须为不超过 {LIMIT} 字的非空文字')
    return value.strip()


def add(work, what, restore, *, scenarios=()):
    """登记一项临时改动：改了什么、怎样恢复，以及当时进行中的场景。"""
    directory = Path(work['dir'])
    with progress.lock(work):
        items = read(directory)
        item = dict(id=len(items) + 1, text=text_of(what, 'text'), restore=text_of(restore, 'restore'), at=now(),
                    scenarios=list(scenarios), restored_at=None)
        progress.atomic_json(directory / FILE, items + [item])
    return item


def restored(work, change_id):
    directory = Path(work['dir'])
    with progress.lock(work):
        items = read(directory)
        item = next((entry for entry in items if entry['id'] == change_id), None)
        if item is None:
            reject(404, 'change-not-found', 'id', f"运行 #{work['id']} 没有临时改动 #{change_id}")
        if item['restored_at'] is None:
            item['restored_at'] = now()
            progress.atomic_json(directory / FILE, items)
    return item


def pending(directory):
    return [item for item in read(directory) if item['restored_at'] is None]


def attention_text(run_id, items):
    return f'运行 #{run_id} 结束时有 {len(items)} 项临时改动没有恢复：' + '；'.join(
        f"#{item['id']} {item['text']}（恢复办法：{item['restore']}）" for item in items)
