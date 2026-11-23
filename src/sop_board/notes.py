"""产品备注（已定决策 31 第五段）：按产品共享的自由文本，挂在产品、入口或场景文件上。

团队想告诉执行会话的事写在这里：怎样登录、数据在哪看、哪些现象是已知噪声、哪些测试对象可以临时改。
看板不预设类别，只保管来源、确认与不符报告；准备运行时把与本轮有关的备注写进 brief。
"""
from .errors import reject
from .store import dumps, loads, now

LIMIT = 2000
PRODUCT_LIMIT = 3000   # 全产品备注每轮都带；合计超过时提醒改挂到入口或场景文件上
FIELDS = 'id, scope, target, text, machine, run, created_by, created_at, confirmed_by, confirmed_at, disputes, removed_by, removed_at'


def clean(text):
    if not isinstance(text, str) or not text.strip():
        reject(422, 'invalid-arguments', 'text', '备注须为非空文字')
    text = text.strip()
    if len(text) > LIMIT:
        reject(422, 'invalid-arguments', 'text', f'一条备注不超过 {LIMIT} 字；内容多时拆成几条')
    return text


def target_of(entry, file, *, entries=(), files=()):
    """备注挂在哪里：入口、场景文件，都不写时是整个产品。"""
    if entry and file:
        reject(422, 'invalid-arguments', 'entry', '--entry 与 --file 只写一个')
    if entry:
        if entry not in entries:
            reject(422, 'entry-not-found', 'entry', f'没有声明入口 {entry}')
        return 'entry', entry
    if file:
        name = file[:-5] if file.endswith('.yaml') else file
        if name not in files:
            reject(422, 'scenario-file-not-found', 'file', f'没有场景文件 {name}')
        return 'file', name
    return 'product', ''


def public(row, machines=None):
    item = dict(id=row['id'], scope=row['scope'], target=row['target'] or None, text=row['text'], created_by=row['created_by'],
                created_at=row['created_at'], run=row['run'],
                machine=(machines or {}).get(row['machine'], row['machine']) if row['run'] is not None else None,
                confirmed_by=row['confirmed_by'], confirmed_at=row['confirmed_at'], disputes=loads(row['disputes'], []))
    if row['removed_at']:
        item.update(removed_by=row['removed_by'], removed_at=row['removed_at'])
    return item


def fetch(db, store, note_id):
    row = db.one(f'SELECT {FIELDS} FROM notes WHERE product=? AND id=?', (store.product, note_id))
    if row is None or row['removed_at']:
        reject(404, 'note-not-found', 'id', f'没有备注 #{note_id}')
    return row


def machines(db):
    return {row['id']: row['name'] for row in db.execute('SELECT id, name FROM machines')}


def product_chars(db, store):
    return sum(len(row['text']) for row in db.all(
        "SELECT text FROM notes WHERE product=? AND scope='product' AND removed_at IS NULL", (store.product,)))


def budget(db, store, scope):
    """全产品备注合计超过上限时的提醒；只在这次操作涉及全产品备注时给出。"""
    total = product_chars(db, store)
    if scope != 'product' or total <= PRODUCT_LIMIT:
        return []
    return [dict(code='product-notes-large', total_chars=total, limit=PRODUCT_LIMIT,
                 problem=f'全产品备注合计 {total} 字，超过 {PRODUCT_LIMIT}：它们每一轮都会带给执行会话。'
                         '只和部分场景有关的，用 sop-board note move <编号> --entry <入口> 或 --file <场景文件> 改挂；'
                         '只对某一次启动有效的安排改用 run launch --note')]


def add(store, text, *, by, scope='product', target='', run=None):
    """人写的备注当即算已确认；执行会话带运行编号提出的标为未经人确认。"""
    text, at = clean(text), now()
    with store.records_tx() as db:
        note_id = db.insert(
            'INSERT INTO notes(product, scope, target, text, machine, run, created_by, created_at, confirmed_by, confirmed_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (store.product, scope, target, text, store.machine['id'], run, by, at,
             None if run is not None else by, None if run is not None else at))
        return dict(ok=True, note=public(fetch(db, store, note_id), machines(db)), warnings=budget(db, store, scope))


def move(store, note_id, scope, target):
    """改挂到别的范围：内容、来源与确认不变。"""
    with store.records_tx() as db:
        fetch(db, store, note_id)
        db.execute('UPDATE notes SET scope=?, target=? WHERE id=?', (scope, target, note_id))
        return dict(ok=True, note=public(fetch(db, store, note_id), machines(db)), warnings=budget(db, store, scope))


def listing(store, *, scope=None, target=None, removed=False):
    sql, params = f'SELECT {FIELDS} FROM notes WHERE product=?', [store.product]
    if scope:
        sql, params = sql + ' AND scope=? AND target=?', params + [scope, target]
    if not removed:
        sql += ' AND removed_at IS NULL'
    with store.records() as db:
        names = machines(db)
        return [public(row, names) for row in db.all(sql + ' ORDER BY id', params)]


def change(store, note_id, *, by, text=None, dispute=None, run=None, remove=False):
    """改写、确认、报告不符或删除一条备注。改写与确认都由人做，并清掉不符报告。"""
    at = now()
    with store.records_tx() as db:
        row = fetch(db, store, note_id)
        if remove:
            db.execute('UPDATE notes SET removed_by=?, removed_at=? WHERE id=?', (by, at, note_id))
            return dict(ok=True, removed=note_id)
        if dispute is not None:
            reports = loads(row['disputes'], []) + [dict(by=by, at=at, text=clean(dispute), run=run,
                                                         machine=store.machine['id'] if run is not None else None)]
            db.execute('UPDATE notes SET disputes=? WHERE id=?', (dumps(reports), note_id))
        else:
            db.execute("UPDATE notes SET text=?, confirmed_by=?, confirmed_at=?, disputes='[]' WHERE id=?",
                       (clean(text) if text is not None else row['text'], by, at, note_id))
        return dict(ok=True, note=public(fetch(db, store, note_id), machines(db)),
                    warnings=budget(db, store, row['scope']) if text is not None else [])


def relevant(store, entries, files):
    """与本轮有关的备注：整个产品的，加上本轮用到的入口与场景文件的。"""
    wanted = {('entry', name) for name in entries} | {('file', name) for name in files}
    return [item for item in listing(store) if item['scope'] == 'product' or (item['scope'], item['target']) in wanted]


def brief_lines(items, run):
    """写进 brief 的备注一节。"""
    lines = ['', '## 产品备注', '']
    if not items:
        lines.append('- 还没有与本轮有关的备注。')
    for item in items:
        where = {'product': '', 'entry': f"[入口 {item['target']}] ", 'file': f"[场景文件 {item['target']}] "}[item['scope']]
        source = (f"{item['confirmed_by']} 确认，{item['confirmed_at'][:10]}" if item['confirmed_by']
                  else f"运行 #{item['run']} 提出，未经人确认")
        line = f"- #{item['id']} {where}{item['text']}（{source}）"
        for report in item['disputes']:
            line += f"\n  - 有人报告与实际不符：{report['text']}（{report['by']}，{report['at'][:10]}）"
        lines.append(line)
    lines += ['',
              '备注说明怎样操作与观察，不提供预期；与实际页面不一致时以实际为准。',
              f'- 本轮学到、下一轮还用得上的产品事实（怎样登录、数据在哪里看、哪些现象是已知噪声）记成备注：'
              f'`sop-board note add --run {run} [--entry <入口> | --file <场景文件>] --text <一条事实>`。只记事实，不记点击路线和元素定位。',
              f'- 某条备注与实际不符：`sop-board note dispute <编号> --run {run} --text <看到的现象>`，然后按实际情况继续。']
    return lines
