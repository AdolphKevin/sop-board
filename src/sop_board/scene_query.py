"""按场景查找当前状态与可见历史。"""
from . import scenarios, status
from .errors import reject


def summary(book, key, scene, info):
    rows = book.select((scene['file'], scene['id'], scene['fingerprint'])).records
    latest = next((row for row in rows if row['kind'] == 'agent'), None)
    return dict(key=key, title=scene['title'], status=info['state'], disposition=info['disposition'],
                refs=scene.get('refs', []),
                latest_result=dict(at=latest['executed_at'], independence=latest['independence']) if latest else None)


def history(book, scene):
    entries = []
    for row in book.history(scene) + book.guard_history(scene):
        kind = row.get('kind', 'guard')
        at, executor, finding = row.get('executed_at'), row.get('marked_by'), row.get('finding')
        commits = None
        if kind == 'agent':
            executor = row['executor']
            work = book.works.get(row['work']) or {}
            repos = (work.get('snapshot') or {}).get('repos') or {}
            commits = {alias: info['commit'] for alias, info in repos.items() if info.get('commit')} or None
        elif kind == 'guard':
            at, executor, finding = row['ran_at'], row['operator'], row['reason']
        item = dict(kind=kind, id=row['id'], at=at, status=row['status'],
                    independence=row['independence'] if kind == 'agent' else None,
                    executor=executor or None, branch=row['branch'], machine=book.machine_name(row['machine']),
                    commits=commits, product_digest=row['product_digest'], fingerprint=row['fingerprint'],
                    current=row['fingerprint'] == scene['fingerprint'], finding=finding, note=row.get('note'),
                    cause=row.get('cause') if kind == 'agent' else None,
                    suspect_defect=bool(row.get('suspect_defect')) if kind == 'agent' else False)
        entries.append((status.instant(at), row['seq'], item))
    entries.sort(key=lambda entry: entry[:2], reverse=True)
    return [entry[2] for entry in entries]


def show(context, key, found, derived):
    book = derived['book']
    if '#' in key:
        scenarios.require_files(found, {key.split('#', 1)[0]})
    try:
        key, = scenarios.resolve([key], found['scenarios'])
    except ValueError as error:
        reject(404, 'scenario-not-found', 'key', str(error))
    scene, info = found['scenarios'][key], derived['items'][key]
    item = summary(book, key, scene, info)
    item.update(reason=info['reason'], badges=dict(distill=info['distill'], review=info['review'],
                                                  reservation=info['reservation'],
                                                  draft_review=info.get('draft_review', {}).get('state')),
                guard={field: info['guard'].get(field) for field in ('valid', 'reason')},
                other_dispositions=book.other_dispositions(key),
                held=[item for item in derived['held'] if item['key'] == key])
    return dict(ok=True, checkout=context.checkout.name, branch=book.viewer.branch, scenario=item, history=history(book, scene))


def listing(context, found, derived, *, state=None, file=None, since=None, ref=None, text=None):
    book = derived['book']
    if file is not None:
        scenarios.require_files(found, {file})
    items = []
    for key, scene in sorted(found['scenarios'].items()):
        info = derived['items'][key]
        if (state is not None and state != info['state']) or (file is not None and file != scene['file']):
            continue
        if ref is not None and ref not in scene.get('refs', []):
            continue
        if text is not None and not any(text in field.casefold() for field in
                                        (scene['title'], *scene['steps'], *scene['expected'])):
            continue
        if since is not None and not any(status.instant(row['executed_at']) >= since for row in book.history(scene)):
            continue
        items.append(summary(book, key, scene, info))
    return dict(ok=True, checkout=context.checkout.name, branch=book.viewer.branch, scenarios=items)
