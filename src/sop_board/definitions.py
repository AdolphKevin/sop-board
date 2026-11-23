"""产品级定义的共享副本（已定决策 31 第五段）。

入口的操作、观察、留证与隔离说明，身份代表的角色，服务与数据配方的业务说明，对同一产品的每个人都一样。
它们随产品存在业务记录库：本机准备运行或在设置页保存时发布，新成员加入产品时用来预填本机配置。
入口的结构与本机绑定（地址、凭据取值、命令、端口）仍以本机 config.yaml 为准，运行也只读本机配置，
共享库连不上时本机命令照常工作。
"""
import json

import yaml

from .store import dumps, loads, now

ENTRY_FIELDS = ('surface', 'operate', 'observe', 'evidence', 'isolation', 'credential_env')


def local(project):
    """本机配置里的产品级定义：{(类别, 名称): 内容}。"""
    found = {}
    for name, entry in (project.entries or {}).items():
        if not isinstance(entry, dict):
            continue
        body = {field: entry[field] for field in ENTRY_FIELDS if field in entry}
        if entry.get('identities'):
            body['identities'] = [{key: item[key] for key in ('id', 'label', 'real') if key in item}
                                  for item in entry['identities'] if isinstance(item, dict)]
        found[('entry', name)] = body
    for name, service in ((project.adapter or {}).get('services') or {}).items():
        if isinstance(service, dict):
            found[('service', name)] = {key: service[key] for key in ('about', 'stoppable') if key in service}
    for recipe in ((project.fixtures or {}).get('recipes') or []):
        if isinstance(recipe, dict) and recipe.get('name'):
            found[('recipe', recipe['name'])] = {key: recipe[key] for key in ('title', 'params', 'creates') if key in recipe}
    return found


def publish(store, project, *, by):
    """把本机的产品级定义写进共享副本：内容变了才改，返回改动的条数。最近一次发布的机器为准。"""
    changed, at = 0, now()
    with store.records_tx() as db:
        current = {(row['kind'], row['name']): loads(row['body'], {})
                   for row in db.all('SELECT kind, name, body FROM definitions WHERE product=?', (store.product,))}
        for (kind, name), body in local(project).items():
            if current.get((kind, name)) == json.loads(dumps(body)):
                continue
            db.execute('INSERT INTO definitions(product, kind, name, body, machine, updated_by, updated_at) VALUES (?,?,?,?,?,?,?) '
                       'ON CONFLICT (product, kind, name) DO UPDATE SET body=excluded.body, machine=excluded.machine, '
                       'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
                       (store.product, kind, name, dumps(body), store.machine['id'], by, at))
            changed += 1
    return changed


def shared(db, product):
    return [dict(kind=row['kind'], name=row['name'], body=loads(row['body'], {}), updated_by=row['updated_by'],
                 updated_at=row['updated_at'])
            for row in db.all('SELECT kind, name, body, updated_by, updated_at FROM definitions WHERE product=? ORDER BY kind, name',
                              (product,))]


def missing_entries(items, config):
    """共享副本里有、本机配置里还没有的入口：{入口名: 预填的入口配置}。"""
    present = config.get('entries') if isinstance(config.get('entries'), dict) else {}
    return {item['name']: item['body'] for item in items if item['kind'] == 'entry' and item['name'] not in present}


def prefill(config, items):
    """把缺的入口写进配置对象，返回 (新配置, 补上的入口名)。服务与配方的说明随输出给出，由写适配与配方命令的人照抄。"""
    added = missing_entries(items, config)
    if not added:
        return config, []
    candidate = dict(config)
    candidate['entries'] = {**(config.get('entries') or {}), **added}
    return candidate, sorted(added)


def write_prefill(project, items):
    """CLI 加入产品后预填本机 config.yaml；原文件留在 config.yaml.prev。"""
    from .settings import atomic
    raw = project.config_path.read_bytes() if project.config_path.exists() else b''
    config = yaml.safe_load(raw) or {}
    if not isinstance(config, dict):
        return []
    candidate, added = prefill(config, items)
    if added:
        atomic(project.config_path.with_name('config.yaml.prev'), raw)
        atomic(project.config_path, yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode())
        project.reload()
    return added
