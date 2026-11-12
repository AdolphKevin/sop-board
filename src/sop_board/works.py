"""运行记录的两部分（已定决策 21）：本机部分在本机状态库的 local_work（目录、prepared/submitted/abandoned、运行编号），
业务部分在业务记录库的 work（范围、执行者与独立性、未执行清单、服务停启记录），以“机器标识 + 本机库标识 + 本机运行编号”唯一，
重复登记不新增行。"""
from .store import dumps, now


def publish(db, store, row, *, checkout, branch, result=None, services=None, implementer=None, implemented_by=None,
            independence=None, ended_at=None, sequence=True):
    """把本机运行写进业务记录库；已经登记过（重新提交）时返回原有编号。返回 (业务编号, 是否新登记)。"""
    machine = store.machine['id']
    created = db.insert(
        'INSERT INTO work(product,machine,origin,local_id,seq,kind,checkout,branch,session,operator,implementer,implemented_by,'
        'independence,fix,launch,scope,snapshot,product_digest,result,services,created_at,ended_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
        (store.product, machine, store.origin, row['id'], db.next_seq() if sequence else None, row['kind'], checkout, branch,
         row['session'], row['operator'], implementer, implemented_by, independence, row['fix'], row['launch'], row['scope'],
         row['snapshot'] or '{}', row['product_digest'], dumps(result) if result is not None else None, dumps(services or []),
         row['created_at'], ended_at or now()))
    if created is not None:
        return created, True
    return existing(db, store, row['id']), False


def existing(db, store, local_id):
    """本机运行在业务记录库中的编号；还没有登记时返回 None。"""
    return db.value('SELECT id FROM work WHERE machine=? AND origin=? AND local_id=?', (store.machine['id'], store.origin, local_id))


def local_dir(store, db, record_id):
    """业务记录库的运行编号 → 本机运行目录（只有本机本库产出的运行才有）。"""
    if not record_id:
        return None
    row = db.one('SELECT machine, origin, local_id FROM work WHERE id=?', (record_id,))
    if row is None or row['machine'] != store.machine['id'] or row['origin'] != store.origin:
        return None
    with store.local() as local:
        found = local.execute('SELECT dir FROM local_work WHERE id=?', (row['local_id'],)).fetchone()
    return found['dir'] if found and found['dir'] else None


def finish(store, row, *, checkout, branch, result):
    """编写类工作（合并、SOP、地图、沉淀）提交时：先登记业务部分，再把本机部分标为 submitted。"""
    with store.records_tx() as db:
        record, _ = publish(db, store, row, checkout=checkout, branch=branch, result=result)
    with store.local_tx() as db:
        db.execute("UPDATE local_work SET status='submitted', ended_at=?, result=?, record=? WHERE id=?",
                   (now(), dumps(result), record, row['id']))
    return record
