"""分支上的验收安排，与场景指纹和验收结果分别记录。"""
from . import config, scenarios
from .errors import reject
from .store import now

LABELS = {'human': '由人验收', 'shelved': '已搁置'}


def load(db, product):
    grouped = {}
    for row in db.execute('SELECT file, scenario, branch, kind, reason, "by", at FROM dispositions WHERE product=?', (product,)):
        item = dict(row)
        key = scenarios.key(item.pop('file'), item.pop('scenario'))
        grouped.setdefault(key, {})[item['branch']] = item
    return grouped


def listed(items, keys=None):
    return [dict(key=key, status=items[key]['state'], **items[key]['disposition'])
            for key in sorted(items if keys is None else keys) if items[key]['disposition']]


def change(context, keys, *, clear=False, kind=None, reason=None):
    from .cli import scene_board
    if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not k.strip() for k in keys):
        reject(422, 'disposition-invalid', 'keys', '须给出至少一个场景键')
    if not clear:
        if not isinstance(kind, str) or kind not in LABELS:
            reject(422, 'disposition-invalid', 'kind', '处置种类应为 human 或 shelved')
        if not isinstance(reason, str) or not reason.strip() or '\x00' in reason:
            reject(422, 'disposition-invalid', 'reason', '须给出非空的处置原因')
        reason = reason.strip()
    found, _, derived = scene_board(context)
    branch = derived['book'].viewer.branch
    if branch is None:
        reject(409, 'disposition-branch-required', 'branch', '当前检出未在分支上，无法更改处置')
    scenes = found['scenarios']
    scenarios.require_files(found, {k.split('#', 1)[0] for k in keys if '#' in k})
    try:
        selected = sorted(scenarios.resolve(keys, scenes))
    except ValueError as error:
        reject(404, 'scenario-not-found', 'keys', str(error))
    store = context.store
    product = store.product
    if clear:
        cleared, unchanged = [], []
        with store.records_tx() as db:
            for key in selected:
                scene = scenes[key]
                cursor = db.execute('DELETE FROM dispositions WHERE product=? AND file=? AND scenario=? AND branch=?',
                                    (product, scene['file'], scene['id'], branch))
                (cleared if cursor.rowcount else unchanged).append(key)
        return dict(ok=True, branch=branch, cleared=cleared, unchanged=unchanged)
    drafts = [k for k in selected if scenes[k]['status'] == 'draft']
    if drafts:
        reject(409, 'draft-not-runnable', 'keys', '草稿确认后才能设置处置：' + '、'.join(drafts))
    by, at = config.require_operator(), now()
    with store.records_tx() as db:
        for key in selected:
            scene = scenes[key]
            db.execute('INSERT INTO dispositions(product,file,scenario,branch,kind,reason,"by",at) VALUES (?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(product,file,scenario,branch) DO UPDATE SET '
                       'kind=excluded.kind, reason=excluded.reason, "by"=excluded."by", at=excluded.at',
                       (product, scene['file'], scene['id'], branch, kind, reason, by, at))
    return dict(ok=True, branch=branch, dispositions=[
        dict(key=key, status=derived['items'][key]['state'], kind=kind, reason=reason, by=by, at=at, branch=branch)
        for key in selected])
