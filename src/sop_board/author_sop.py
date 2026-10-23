"""业务 SOP 编写：冻结资料、保存真人答复，经确认后复用原文登记。"""
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re

from .drafts import schema_problems
from .errors import reject
from .kernel import InputChanged, Inputs, S
from .service import canonical, now
from .time_format import local_time
from .validation import validate_sop_upload


SCHEMA = json.loads(files('sop_board').joinpath('prompts/author-sop.schema.json').read_text(encoding='utf-8'))
HEADINGS = ('业务目标与范围', '角色', '业务规则', '流程图', '禁止的副作用', '未覆盖范围')


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_files(paths):
    if not paths or len(paths) > 32:
        raise ValueError('请提供 1～32 份来源资料')
    result, contents, seen = [], {}, set()
    for number, path in enumerate(paths, 1):
        path = Path(path).expanduser().resolve()
        if path in seen or not path.is_file():
            raise ValueError('来源资料不存在、不是文件或重复：' + str(path))
        seen.add(path)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('单份来源资料不能超过 20 MiB：' + path.name)
        raw = path.read_bytes()
        if not raw:
            raise ValueError('来源资料不能为空：' + path.name)
        sid = f'S{number}'
        # 用编号隔离同名资料；展示名称不携带原文件的本机路径。
        relative = f'sources/{sid}/{path.name}'
        result.append(dict(id=sid, filename=path.name, path=relative, original_path=str(path),
                           sha256=hashlib.sha256(raw).hexdigest(), size=len(raw)))
        contents[relative] = raw
    return result, contents


def quoted(value):
    return '\n'.join('> ' + line for line in value.splitlines())


class AuthorSop:
    def __init__(self, tasks):
        self.tasks, self.store, self.service = tasks, tasks.store, tasks.service

    def inputs(self, task):
        return Inputs(json.loads(task['input_json']), dict(config_digest='工作区配置', base_sha256='批次 SOP 原文'))

    def enqueue(self, batch, sources, contents):
        workspace = self.service.workspace
        snapshot = dict(batch=batch, config_digest=workspace.config_digest, base_sha256=self.service.sop_sha256(batch),
                        sources=sources, repos={alias: str(path) for alias, path in workspace.repos.items()})
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO batches(workspace,name,path,product_digest) VALUES (?,?,'','')", (workspace.name,batch))
            tid = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at) "
                "VALUES ('author-sop',?,?,?,'会话编写 SOP',?,?,?)",
                (workspace.name,batch,S.QUEUED,digest(snapshot),canonical(snapshot),now())).lastrowid
            directory = self.tasks.directory(tid)
            for relative, raw in contents.items():
                path = directory / relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
        return tid

    def current(self, task):
        workspace = self.service.workspace
        workspace.reload()
        changes = self.inputs(task).changed(dict(config_digest=workspace.config_digest,
            base_sha256=self.service.sop_sha256(task['batch'])))
        directory = self.tasks.directory(task['id'])
        for source in json.loads(task['input_json'])['sources']:
            path = directory / source['path']
            if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory)
                    or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']):
                changes.append('来源副本 ' + source['id'])
        if changes:
            raise InputChanged('、'.join(changes) + '已变化，请重新准备编写任务')

    def answers(self, task_id, *, connection=None):
        if connection is None:
            with self.store.connection() as db:
                return self.answers(task_id,connection=db)
        return [dict(row) for row in connection.execute(
            'SELECT id,question,text,author,answered_at FROM author_sop_answers WHERE task=? ORDER BY id', (task_id,))]

    def prepare(self, task):
        self.current(task)
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True,exist_ok=True)
        snapshot = json.loads(task['input_json'])
        for name, value in [('input.json',snapshot),('sources.json',snapshot['sources']),('repos.json',snapshot['repos']),('schema.json',SCHEMA)]:
            (directory/name).write_text(canonical(value),encoding='utf-8')
        self.write_answers(task)
        guide = directory/'sop-guide.md'
        guide.write_text(files('sop_board').joinpath('prompts/author-sop-guide.md').read_text(encoding='utf-8'),encoding='utf-8')
        prompt = files('sop_board').joinpath('prompts/author-sop.md').read_text(encoding='utf-8')
        prompt += '\n本轮任务目录：' + str(directory) + '\n本轮编写规则：' + str(guide) + '\n'
        (directory/'prompt.md').write_text(prompt,encoding='utf-8')
        return prompt

    def write_answers(self, task):
        answers = self.answers(task['id'])
        value = dict(answers=answers,sha256=digest(answers))
        (self.tasks.directory(task['id'])/'answers.json').write_text(canonical(value),encoding='utf-8')
        return value

    def read_output(self, task):
        from .tasks import judgment_object, invalid_judgment_constant
        directory = self.tasks.directory(task['id'])
        path = directory/'final.json'
        if path.is_symlink() or not path.resolve().is_relative_to(directory) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('final.json 必须为任务目录内不超过 2 MiB 的普通文件')
        result = json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=judgment_object,parse_constant=invalid_judgment_constant)
        problems = schema_problems(result,SCHEMA)
        if problems:
            raise ValueError('输出模式：' + '；'.join(problems))
        markdown = result['sop_markdown']
        if not markdown.strip() or len(markdown.encode()) > 1024 * 1024:
            raise ValueError('sop_markdown 须为不超过 1 MiB 的非空 Markdown')
        for heading in HEADINGS:
            if not re.search(r'^##\s+' + heading + r'\s*$',markdown,re.M):
                problems.append('SOP 缺少章节：' + heading)
        if not re.search(r'^```mermaid\s*\n\s*(?:flowchart|graph)\s+\w+',markdown,re.M):
            problems.append('SOP 须包含主流程与分支的 mermaid 流程图')
        sources = {row['id']:row for row in json.loads(task['input_json'])['sources']}
        seen = set()
        for rule in result['rules']:
            rid = rule['id']
            if not re.fullmatch(r'R[1-9][0-9]*',rid) or rid in seen:
                problems.append('规则编号不合格或重复：' + rid)
            seen.add(rid)
            if not re.search(r'^###\s+' + re.escape(rid) + r'(?:\s|[、：:.])',markdown,re.M):
                problems.append('SOP 缺少对应规则小节：' + rid)
        if not seen:
            problems.append('至少写一条业务规则，无法确认的内容标为待确认')
        if set(re.findall(r'^###\s+(R[1-9][0-9]*)(?=\s|[、：:.])',markdown,re.M)) != seen:
            problems.append('正文规则与 rules 来源清单不一致')
        seen_questions = set()
        for question in result['questions']:
            qid = question['id']
            if not re.fullmatch(r'Q[1-9][0-9]*',qid) or qid in seen_questions:
                problems.append('问题编号不合格或重复：' + qid)
            seen_questions.add(qid)
            for field in ('title','scenario','current','missing','question'):
                if not question[field].strip():
                    problems.append(qid + ' 缺少 ' + field)
            if not question['affected'] or any(not p.strip() for p in question['affected']):
                problems.append(qid + ' 缺少涉及内容')
        for item in result['rules'] + result['questions']:
            if not item['sources']:
                problems.append(item['id'] + ' 缺少来源文件与原文摘录')
            for ref in item['sources']:
                source = sources.get(ref['source'])
                if not source or not ref['quote'].strip():
                    problems.append(item['id'] + ' 的来源编号或摘录不合格')
                    continue
                try:
                    original = (directory/source['path']).read_text(encoding='utf-8')
                except UnicodeError:
                    # 二进制资料仍原样固定，摘录的语义及真实性由独立审阅核对。
                    continue
                if re.sub(r'\s+','',ref['quote']) not in re.sub(r'\s+','',original):
                    problems.append(item['id'] + ' 摘录不在来源副本 ' + ref['source'] + ' 中')
        if problems:
            raise ValueError('；'.join(problems))
        return result

    def review(self, task):
        self.current(task)
        result = self.read_output(task)
        with self.store.transaction() as db:
            previous = {r['number']:json.loads(r['body_json']) for r in db.execute(
                'SELECT number,body_json FROM author_sop_questions WHERE task=?',(task['id'],))}
            questions = {q['id']:q for q in result['questions']}
            if any(questions.get(qid) != question for qid,question in previous.items()):
                raise ValueError('已呈报的问题不能删除或换义；补充问题使用新编号，答复只针对原问题')
            for qid,question in questions.items():
                db.execute('INSERT OR IGNORE INTO author_sop_questions VALUES (?,?,?,?)',
                    (task['id'],qid,canonical(question),now()))
        (self.tasks.directory(task['id'])/'questions.json').write_text(canonical(result['questions']),encoding='utf-8')
        self.write_answers(task)
        self.preview(task,result)
        return result

    def answer(self, task, question, text, author):
        if not text.strip() or len(text) > 20000 or '\0' in text:
            raise ValueError('业务答复须为 1～20000 字非空原文')
        result = self.review(task)
        if question not in {q['id'] for q in result['questions']}:
            reject(404,'sop-question-not-found','question','该问题不属于本次任务')
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM author_sop_answers WHERE task=? AND question=? ORDER BY id DESC LIMIT 1',
                (task['id'],question)).fetchone()
            duplicate = bool(previous and previous['text']==text and previous['author']==author)
            if not duplicate:
                db.execute('INSERT INTO author_sop_answers(task,question,text,author,answered_at) VALUES (?,?,?,?,?)',
                    (task['id'],question,text,author,now()))
        answers = self.write_answers(task)
        self.preview(task,result)
        return dict(ok=True,question=question,duplicate=duplicate,answers_sha256=answers['sha256'],
                    guidance='读取 answers.json，按答复修订 final.json 的正文和 applied_answers，再呈报使用者确认')

    def preview(self, task, result, confirmed_by=None):
        snapshot = json.loads(task['input_json'])
        sources = {s['id']:s for s in snapshot['sources']}
        answers = self.answers(task['id'])
        parts = [result['sop_markdown'].rstrip(),'\n## 规则来源与原文摘录']
        for rule in result['rules']:
            parts.append('\n### ' + rule['id'] + ' 的依据')
            for ref in rule['sources']:
                parts += [sources[ref['source']]['filename'] + '（' + ref['source'] + '）：',quoted(ref['quote'])]
        parts.append('\n## 问题与业务答复')
        for question in result['questions']:
            parts.append('\n### ' + question['id'] + '：' + question['title'])
            for field,label in [('scenario','场景'),('current','目前的写法'),('missing','缺什么'),('question','请确认')]:
                parts += [label + '：',quoted(question[field])]
            parts.append('涉及内容：' + '、'.join(question['affected']))
            for ref in question['sources']:
                parts += [sources[ref['source']]['filename'] + '：',quoted(ref['quote'])]
            replies = [a for a in answers if a['question']==question['id']]
            if not replies:
                parts.append('待使用者答复。')
            for answer in replies:
                parts += ['答复人：' + answer['author'] + '；时间：' + local_time(answer['answered_at']),quoted(answer['text'])]
        if not result['questions']:
            parts.append('本轮没有待确认问题。' if confirmed_by else '本轮没有待确认问题；仍须使用者确认后登记。')
        parts.append('\n## 来源资料清单')
        for source in sources.values():
            parts.append(f'- {source["id"]}：{source["filename"]}；SHA-256：{source["sha256"]}；{source["size"]} 字节')
        parts.append('\n业务确认人：' + (confirmed_by or '尚未确认'))
        content = '\n\n'.join(parts) + '\n'
        (self.tasks.directory(task['id'])/'sop.md').write_text(content,encoding='utf-8')
        return content

    def consume(self, task, session, *, confirmed_by, upload):
        result = self.review(task)
        answers = self.answers(task['id'])
        answered = {a['question'] for a in answers}
        missing = {q['id'] for q in result['questions']} - answered
        if missing:
            reject(409,'sop-answers-required','questions','待确认问题尚未答复：' + '、'.join(sorted(missing)))
        if result['answers_sha256'] != digest(answers):
            reject(409,'sop-answers-changed','answers_sha256','答复已更新，请读取 answers.json 并修订正文后再提交')
        applied = result['applied_answers']
        if (len(applied) != len(answered) or {a['question'] for a in applied} != answered or
                any(not a['excerpt'].strip() or a['excerpt'] not in result['sop_markdown'] for a in applied)):
            reject(422,'sop-answers-not-applied','applied_answers','每个已答问题须引用修订后的正文原文，包含范围决定')
        content = self.preview(task,result,confirmed_by)
        body = dict(filename=task['batch']+'.md',content=content,uploaded_by=confirmed_by,
                    confirmed_by=confirmed_by,base_sha256=json.loads(task['input_json'])['base_sha256'])
        validate_sop_upload(body)

        def registered(db, version):
            # 来源和答复随 SOP 版本在同一事务登记；崩溃后也能按任务恢复收据。
            sources = [{k:s[k] for k in ('id','filename','sha256','size')} for s in json.loads(task['input_json'])['sources']]
            db.execute('INSERT INTO author_sop_registrations VALUES (?,?,?,?,?,?)',
                (task['id'],version['id'],canonical(sources),canonical(answers),confirmed_by,now()))
            check = dict(ok=True,version=version['version'],sha256=hashlib.sha256(content.encode()).hexdigest(),confirmed_by=confirmed_by)
            self.tasks.kernel.register(task,check,connection=db,phase='sop-registration')
            db.execute('UPDATE tasks SET status=?,finished_at=?,error=?,result_json=?,result_digest=?,session_id=? WHERE id=?',
                (S.COMPLETED,now(),'',canonical(check),digest(result),session,task['id']))

        return upload(task['batch'],body,on_registered=registered)

    def registered(self, task):
        return self.tasks.kernel.receipt(task,phase='sop-registration')
