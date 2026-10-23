"""批次澄清：人的原文、排队条件与经完整核对后的一次登记。"""
from copy import deepcopy
import hashlib
import json
import re

import yaml

from .cases import load_cases
from .errors import APIError, reject
from .kernel import OUTPUT_ACTIVE, S, sql_states
from .merge_state import read_holds
from .service import canonical, now
from .time_format import local_date


ACTIONS = dict(adopted='已采纳', question='已追问', not_applicable='不适用')


class MergeConversation:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store

    def snapshot(self, db, batch):
        messages = []
        for row in db.execute('SELECT * FROM merge_messages WHERE batch=? ORDER BY id', (batch['id'],)):
            item = dict(row)
            item.pop('batch')
            item['gap_snapshot'] = json.loads(item.pop('gap_json'))
            item['cases'] = json.loads(item.pop('cases_json'))
            item['pending'] = item['status'] == '待处理'
            messages.append(item)
        exclusions = [dict(r) for r in db.execute('SELECT case_id,message FROM merge_exclusions WHERE batch=? ORDER BY case_id', (batch['id'],))]
        used = {mid for r in db.execute('SELECT messages_json FROM merge_case_updates WHERE batch=?', (batch['id'],)) for mid in json.loads(r[0])}
        sources = [dict(ref=f"看板合并澄清 #{m['id']}", version=m['created_at'], confirmed_by=m['author']) for m in messages if m['id'] in used]
        return dict(messages=messages, exclusions=exclusions, sources=sources, holds=read_holds(db, batch))

    def read(self, key, name):
        with self.store.connection() as db:
            db.execute('BEGIN')
            batch = self.service.batch(db, key, name)
            history = self.snapshot(db, batch)
            rounds = []
            for row in db.execute("SELECT id,status,created_at,finished_at,result_json FROM tasks WHERE workspace=? AND batch=? AND kind='merge' ORDER BY id", (key, name)):
                result = json.loads(row['result_json'])
                # Failed or stale output is not a business answer.
                if row['status'] in (S.COMPLETED, S.NEEDS_INPUT):
                    rounds.append(dict(task=row['id'], created_at=row['finished_at'] or row['created_at'],
                        reply=result.get('reply', ''), responses=result.get('responses', []), gaps=result.get('gaps', [])))
            current = db.execute("SELECT id,status,trigger FROM tasks WHERE workspace=? AND batch=? AND kind='merge' ORDER BY id DESC LIMIT 1", (key, name)).fetchone()
        return dict(ok=True, **history['holds'], messages=history['messages'], exclusions=history['exclusions'],
                    rounds=rounds, current=dict(current) if current else None)

    def active(self, db, batch):
        return db.execute(f"SELECT id FROM tasks WHERE workspace=? AND batch=? AND kind='merge' AND status IN ({sql_states(OUTPUT_ACTIVE | {S.SESSION})}) LIMIT 1",
                          (batch['workspace'], batch['name'])).fetchone()

    def request(self, db, batch, session=None):
        db.execute('INSERT INTO merge_followups(batch,requested) VALUES (?,1) ON CONFLICT(batch) DO UPDATE SET requested=requested+1', (batch['id'],))
        active = self.active(db, batch)
        claim = self.tasks.kernel.claim(dict(id=active['id']), connection=db) if active else None
        owner = session or (claim['session_id'] if claim else None)
        if owner:
            db.execute('INSERT INTO session_merge_followups VALUES (?,?) ON CONFLICT(batch) DO UPDATE SET session_id=excluded.session_id', (batch['id'], owner))
        if active:
            return None
        return self.enqueue_followup(db, batch)

    def enqueue_followup(self, db, batch):
        owner = db.execute('SELECT session_id FROM session_merge_followups WHERE batch=?', (batch['id'],)).fetchone()
        result = self.tasks.enqueue_merge(batch['workspace'], batch['name'], trigger='补充澄清', connection=db, fresh=bool(owner))
        if result and owner:
            if result['task']['status'] == S.QUEUED:
                self.tasks.kernel.assign(result['task']['id'], owner[0], connection=db)
                result['task'] = self.tasks.read(result['task']['id'], connection=db)
            db.execute('DELETE FROM session_merge_followups WHERE batch=?', (batch['id'],))
        return result

    def continue_waiting(self):
        with self.store.connection() as db:
            batches = [dict(r) for r in db.execute('SELECT b.* FROM batches b JOIN merge_followups f ON f.batch=b.id '
                'WHERE f.requested>f.dispatched AND NOT EXISTS (SELECT 1 FROM tasks t '
                f"WHERE t.workspace=b.workspace AND t.batch=b.name AND t.kind='merge' AND t.status IN ({sql_states(OUTPUT_ACTIVE | {S.SESSION})}))")]
        for batch in batches:
            with self.store.transaction() as db:
                if not self.active(db, batch) and self.service.workspace.configured:
                    self.enqueue_followup(db, batch)

    def decision_snapshot(self, snapshot):
        working = deepcopy(snapshot)
        excluded = set(snapshot.get('excluded_cases', []))
        if not excluded or not working['scenarios']:
            return working
        document = yaml.safe_load(working['scenarios'])
        unchanged = {s['id'] for n in document['sops'] for s in n['scenarios']
                     if s['id'] not in excluded and not excluded.intersection(s.get('refs', []))}
        for node in document['sops']:
            node['scenarios'] = [s for s in node['scenarios'] if s['id'] not in excluded]
            for scene in node['scenarios']:
                scene['refs'] = [r for r in scene.get('refs', []) if r not in excluded]
                scene['expected'] = [v for v in scene['expected'] if not any(v.startswith(f'[用例 {cid}]') for cid in excluded)]
        document['sops'] = [n for n in document['sops'] if n['scenarios']]
        from .drafts import preserve_scenario_text
        working['scenarios'] = preserve_scenario_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                                                     snapshot['scenarios'], unchanged)
        working['prior_pairs'] = [p for p in snapshot.get('prior_pairs', []) if p['case'] not in excluded and not excluded.intersection(p['scenarios'])]
        return working

    def author(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('author'), str) or not body['author'].strip():
            reject(422, 'clarification-invalid', 'author', '请填写标记人')
        return body['author'].strip()

    def gap(self, db, batch, reference):
        if not isinstance(reference, str) or not re.fullmatch(r'[1-9][0-9]*:G[1-9][0-9]*', reference):
            return None
        task_id, gid = reference.split(':')
        holds = read_holds(db, batch)
        if str(holds['task']) != task_id:
            return None
        return next((g for g in holds['gaps'] if isinstance(g, dict) and g['id'] == gid), None)

    def submit(self, key, name, body, *, source='看板', session=None):
        if source not in ('看板', '会话'):
            raise ValueError('回复来源不合格')
        author = self.author(body)
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages:
            reject(422, 'clarification-invalid', 'messages', '请填写至少一条回复')
        with self.tasks.lock, self.store.transaction() as db:
            batch = self.service.batch(db, key, name)
            if not self.service.case_union(db, batch):
                reject(409, 'cases-missing', 'messages', '批次没有用例，无法补充澄清')
            problems, checked = [], []
            for index, item in enumerate(messages):
                prefix = f'messages.{index}'
                if not isinstance(item, dict):
                    problems.append(dict(field=prefix, problem='应为对象'))
                    continue
                raw = item.get('text')
                if not isinstance(raw, str) or not raw.strip() or len(raw) > 4000:
                    problems.append(dict(field=prefix + '.text', problem='回复应为非空文字，且不超过 4000 字'))
                reference = item.get('gap')
                gap = self.gap(db, batch, reference) if reference is not None else None
                if 'gap' not in item or reference is not None and gap is None:
                    problems.append(dict(field=prefix + '.gap', problem='请选择当前缺口，或用 null 表示通用说明'))
                checked.append((reference, gap, raw))
            if problems:
                raise APIError(422, 'clarification-invalid', problems)
            ids = [db.execute('INSERT INTO merge_messages(batch,author,created_at,gap,gap_json,text,source) VALUES (?,?,?,?,?,?,?)',
                (batch['id'], author, now(), ref, canonical(gap), raw, source)).lastrowid for ref, gap, raw in checked]
            queued = self.request(db, batch, session)
        return dict(ok=True, messages=ids, task=self.tasks.types['merge'].visibility.project(queued['task']) if queued else None,
                    notice='已入队补充澄清' if queued else '已保存，将在当前合并结束后发送')

    def exclude(self, key, name, body, *, include=None, source='看板', session=None):
        if source not in ('看板', '会话'):
            raise ValueError('回复来源不合格')
        author = self.author(body)
        with self.tasks.lock, self.store.transaction() as db:
            batch = self.service.batch(db, key, name)
            cases = {c['id'] for c in self.service.case_union(db, batch)}
            if include is None:
                gap = self.gap(db, batch, body.get('gap'))
                if not gap or gap['kind'] != 'entry':
                    reject(422, 'clarification-invalid', 'gap', '请选择当前由开发者处理的缺口')
                reason = body.get('reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
                    reject(422, 'clarification-invalid', 'reason', '请填写不超过 4000 字的理由')
                affected, operation, raw = gap['cases'], 'exclude', reason
            else:
                if include not in cases or not db.execute('SELECT 1 FROM merge_exclusions WHERE batch=? AND case_id=?', (batch['id'], include)).fetchone():
                    reject(409, 'case-not-excluded', 'case', '用例未被标为本批不纳入')
                gap, affected, operation, raw = None, [include], 'include', '重新纳入本批'
                if source == '会话':
                    reason = body.get('reason')
                    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
                        reject(422, 'clarification-invalid', 'reason', '请填写不超过 4000 字的重新纳入理由')
                    raw += '：' + reason
            mid = db.execute('INSERT INTO merge_messages(batch,author,created_at,gap,gap_json,text,status,source,operation,cases_json,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (batch['id'], author, now(), body.get('gap') if gap else None, canonical(gap), raw, '已采纳', source, operation, canonical(affected), '由回复人决定')).lastrowid
            for cid in affected:
                if include is None:
                    db.execute('INSERT INTO merge_exclusions VALUES (?,?,?) ON CONFLICT(batch,case_id) DO UPDATE SET message=excluded.message', (batch['id'], cid, mid))
                else:
                    db.execute('DELETE FROM merge_exclusions WHERE batch=? AND case_id=?', (batch['id'], cid))
            queued = self.request(db, batch, session)
        return dict(ok=True, message=mid, task=self.tasks.types['merge'].visibility.project(queued['task']) if queued else None,
                    notice='已保存，将按本次决定重新合并')

    def validate(self, snapshot, result, task, db, batch):
        """先在内存中核对完整回复和用例；失败不改变数据库。"""
        history = snapshot.get('clarifications', {})
        messages = {m['id']: m for m in history.get('messages', [])}
        pending = {mid for mid, m in messages.items() if m['pending']}
        problems, responses = [], {}
        for response in result['responses']:
            mid = response['message']
            if mid not in pending or mid in responses:
                problems.append(f'responses 未知、非本轮待处理或重复回复：#{mid}')
            if not response['note'].strip():
                problems.append(f'回复 #{mid} 的处理说明不能为空')
            responses[mid] = response
        problems += [f'responses 漏答回复：#{mid}' for mid in sorted(pending - responses.keys())]
        adopted = {mid for mid, m in messages.items() if m['status'] == '已采纳' and m['operation'] == 'reply'}
        adopted |= {mid for mid, r in responses.items() if r['action'] == 'adopted' and mid in pending}
        working = deepcopy(snapshot)
        cases = {c['id']: c for c in working['cases']}
        updates, seen, used = [], set(), set()
        for update in result['case_updates']:
            cid = update['case']
            changes = {k: v for k, v in update['changes'].items() if v is not None}
            mids = update['messages']
            if cid not in cases or cid in seen:
                problems.append(f'case_updates 用例不存在、已本批不纳入或重复：{cid}')
            seen.add(cid)
            if not mids or len(set(mids)) != len(mids) or any(mid not in adopted for mid in mids):
                problems.append(f'{cid} 的用例更新引用了不存在或未采纳的回复')
            for mid in mids:
                message = messages.get(mid)
                if message and message.get('gap') is not None and cid not in (message.get('gap_snapshot') or {}).get('cases', []):
                    problems.append(f'{cid} 不在回复 #{mid} 对应缺口的关联用例中，不能据此更新')
            if not update['reason'].strip():
                problems.append(f'{cid} 的更新理由不能为空')
            if not changes or cid in cases and not any(cases[cid].get(k) != v for k, v in changes.items()):
                problems.append(f'{cid} 的更新与当前用例没有不同')
            if cid not in cases or any(mid not in adopted for mid in mids):
                continue
            before = deepcopy(cases[cid])
            cases[cid].update(changes)
            basis = '；'.join(f"#{mid}（{messages[mid]['author']}，{local_date(messages[mid]['created_at'])}）" for mid in mids)
            cases[cid]['description'] = before.get('description', '').rstrip() + '\n澄清依据：' + basis
            updates.append(dict(case=cid, before=before, after=deepcopy(cases[cid]), messages=mids, reason=update['reason']))
            used.update(mids)
        if updates:
            try:
                load_cases(dict(filename='合并澄清', uploaded_by='核对', content=yaml.safe_dump(dict(metadata=working['case_metadata'], test_cases=working['cases']), allow_unicode=True)))
            except APIError as error:
                problems += error.problems
            working['upload'] = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM case_uploads WHERE batch=?', (batch['id'],)).fetchone()[0]
        working['clarification_sources'] = list(history.get('sources', []))
        for mid in sorted(used):
            source = dict(ref=f'看板合并澄清 #{mid}', version=messages[mid]['created_at'], confirmed_by=messages[mid]['author'])
            if source not in working['clarification_sources']:
                working['clarification_sources'].append(source)
        # 没有新增操作能力，文字回复不能把之前的入口缺口消掉或替换为业务问题。
        prior_entries = snapshot.get('entry_gaps', {})
        for gap in prior_entries.get('gaps', []):
            affected = set(gap['cases']) & cases.keys()
            if affected and prior_entries.get('entries') == snapshot['entries']:
                covered = {cid for g in result['gaps'] if g['kind'] == 'entry' for cid in g['cases']}
                if not affected <= covered:
                    problems.append('入口类缺口仍缺少操作能力，不能仅凭文字回复消除：' + '、'.join(sorted(affected - covered)))
        return working, dict(updates=updates, responses=list(responses.values()), messages=messages), problems

    def register(self, db, batch, task, working, plan):
        updates = plan['updates']
        if updates:
            number = working['upload']
            turn = db.execute('SELECT COUNT(DISTINCT task)+1 FROM merge_case_updates WHERE batch=?', (batch['id'],)).fetchone()[0]
            mids = list(dict.fromkeys(mid for u in updates for mid in u['messages']))
            author = '、'.join(dict.fromkeys(plan['messages'][mid]['author'] for mid in mids))
            raw = yaml.safe_dump(dict(metadata=working['case_metadata'], test_cases=[u['after'] for u in updates]), allow_unicode=True)
            db.execute('INSERT INTO case_uploads(batch,number,filename,uploaded_by,uploaded_at,sha256,metadata,case_count,added,updated) VALUES (?,?,?,?,?,?,?,?,0,?)',
                (batch['id'], number, f"合并澄清 第 {turn} 轮（任务 #{task['id']}）", author, now(), hashlib.sha256(raw.encode()).hexdigest(), canonical(working['case_metadata']), len(updates), len(updates)))
            for u in updates:
                db.execute('INSERT INTO case_versions(batch,case_id,upload,body) VALUES (?,?,?,?)', (batch['id'], u['case'], number, canonical(u['after'])))
                db.execute('INSERT INTO merge_case_updates VALUES (?,?,?,?,?,?,?,?)',
                    (task['id'], batch['id'], u['case'], number, canonical(u['messages']), canonical(u['before']), canonical(u['after']), u['reason']))
        for response in plan['responses']:
            db.execute('UPDATE merge_messages SET status=?,task=?,note=? WHERE id=? AND batch=? AND status=?',
                (ACTIONS[response['action']], task['id'], response['note'], response['message'], batch['id'], '待处理'))
        direct = [m['id'] for m in plan['messages'].values() if m['operation'] != 'reply' and m['task'] is None]
        for mid in direct:
            db.execute('UPDATE merge_messages SET task=? WHERE id=? AND task IS NULL', (task['id'], mid))
