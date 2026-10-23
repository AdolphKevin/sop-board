"""已核对的暂缓状态；失败或运行中的新任务不抹掉上一轮未解决的问题。"""
import json

from .kernel import MERGE_GAPS, sql_states


def read_holds(db, batch):
    row = db.execute("SELECT id,result_json FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
                     f"AND status IN ({sql_states(MERGE_GAPS)}) ORDER BY id DESC LIMIT 1",
                     (batch['workspace'], batch['name'])).fetchone()
    if row is None:
        return dict(task=None, gaps=[], held=[])
    result = json.loads(row['result_json'])
    excluded = {r[0] for r in db.execute('SELECT case_id FROM merge_exclusions WHERE batch=?', (batch['id'],))}
    gaps = [dict(g, cases=[c for c in g['cases'] if c not in excluded]) if isinstance(g, dict) else g
            for g in result.get('gaps', []) if not isinstance(g, dict) or set(g['cases']) - excluded]
    ids = {g['id'] for g in gaps if isinstance(g, dict)}
    held = [h for h in result.get('held', []) if h['case'] not in excluded and (not h.get('gaps') or ids.intersection(h['gaps']))]
    return dict(task=row['id'], gaps=gaps, held=held)


def held_scenarios(holds):
    # 旧记录没有 retention，沿用旧版“整条场景暂缓”的含义。
    return {sid for item in holds['held'] if item.get('retention', 'scenario') == 'scenario'
            for sid in item.get('scenarios', [])}
