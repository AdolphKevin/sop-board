"""所有 HTTP 与后续页面共用的业务操作。"""

from datetime import datetime, timezone
import hashlib
import json
from itertools import islice
from io import BytesIO
import re
import threading
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

from .errors import APIError, reject
from .cases import load_cases
from .defect_links import normalize
from .evidence_types import content_type, is_text
from .scenario_file import load_scenarios
from .reports import markdown
from .result_files import export_results, load_results
from .statistics import calculate, ratio
from .validation import Problems, validate_mark, validate_push, validate_round


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(scenario):
    value = {key: [text.replace("\r\n", "\n").replace("\r", "\n") for text in scenario[key]]
             for key in ("initial_state", "steps", "expected", "forbidden_effects")}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def project_key(identity):
    return re.sub("[^a-z0-9]+", "-", identity.lower()).strip("-")


class Service:
    def __init__(self, store, jira_url="", writable_repos=None):
        self.store = store
        self.writable_repos = writable_repos or {}
        self.workspace_problems = {}
        self.merge_callback = None
        # 发现与合并落地共享锁，防止看板写入在登记版本前被自己的扫描发现。
        self.workspace_lock = threading.RLock()
        self.jira_url = jira_url.rstrip("/")

    def upload(self, raw, name):
        with self.store.transaction() as db:
            return self.store.save_evidence(db, raw, name)

    def evidence(self, sha):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM evidence WHERE sha256=?", (sha,)).fetchone()
            if not row:
                reject(404, "evidence-not-found", "sha256", "证据不存在")
            return (self.store.data / "evidence" / sha[:2] / sha).read_bytes(), row["name"]

    def batch(self, db, key, name):
        if not db.execute("SELECT 1 FROM projects WHERE key=?", (key,)).fetchone():
            reject(404, "project-not-found", "project", "项目不存在")
        batch = db.execute("SELECT * FROM batches WHERE project=? AND name=?", (key, name)).fetchone()
        if not batch:
            reject(404, "batch-not-found", "batch", "批次不存在")
        return batch

    def latest_version(self, db, batch):
        return db.execute("SELECT * FROM versions WHERE batch=? ORDER BY version DESC LIMIT 1", (batch["id"],)).fetchone()

    def batch_etag(self, db, batch):
        numbers = [db.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table} WHERE batch=?",
                              (batch["id"],)).fetchone()[0]
                   for table, column in [("versions", "version"), ("rounds", "number"), ("case_uploads", "number")]]
        etag = 'v%d-r%d-u%d' % tuple(numbers)
        problems = self.workspace_problems.get((batch['project'], batch['name']), [])
        if problems:
            etag += '-w' + hashlib.sha256(canonical(problems).encode()).hexdigest()[:12]
        return '"' + etag + '"'

    def state(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            return dict(ok=True, etag=self.batch_etag(db, batch))

    def report_data(self, db, batch):
        summary = calculate(db, batch)
        cases = self.case_union(db, batch)
        ids = {s["scenario"] for s in summary["scenarios"]}
        merge = db.execute("SELECT id,result_json FROM tasks WHERE project=? AND batch=? AND kind='merge' AND status='已完成' ORDER BY id DESC LIMIT 1",
                           (batch['project'], batch['name'])).fetchone()
        return dict(batch=dict(batch), project=dict(db.execute("SELECT * FROM projects WHERE key=?", (batch["project"],)).fetchone()),
                    merge=dict(id=merge['id'], **json.loads(merge['result_json'])) if merge else None,
                    version=self.latest_version(db, batch), summary=summary, cases=cases,
                    coverage=ratio(sum(c["id"] in ids for c in cases), len(cases)),
                    latest_upload=db.execute("SELECT COALESCE(MAX(number),0) FROM case_uploads WHERE batch=?", (batch["id"],)).fetchone()[0])

    def report(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            data = self.report_data(db, self.batch(db, key, name))
            return markdown(data, datetime.now().astimezone().isoformat()).encode()

    def results_yaml(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            return export_results(self.report_data(db, self.batch(db, key, name)), self.jira_url)

    def import_results(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            current = {row["scenario"]: row["fingerprint"] for row in db.execute(
                "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch["id"],))}
            prefix = db.execute("SELECT defect_prefix FROM projects WHERE key=?", (key,)).fetchone()[0]
            document, prepared, skipped = load_results(body, current, self.jira_url, prefix)
            if not prepared:
                return dict(ok=True, round=None, imported=0, skipped=skipped)
            version = self.latest_version(db, batch)
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
            latest_upload = db.execute("SELECT COALESCE(MAX(number),0) FROM case_uploads WHERE batch=?", (batch["id"],)).fetchone()[0]
            filename = f"import-{number}.json"
            for result, original in prepared:
                record = dict(uploaded_by=body["uploaded_by"], run_id=document["run_id"], round=document["round"],
                              environment=document["environment"], latest_upload=latest_upload, result=original)
                saved = self.store.save_evidence(db, canonical(record).encode(), filename)
                result["evidence"] = [dict(name=filename, sha256=saved["sha256"])]
            round_body = dict(product=dict(commit=version["commit_id"], digest=batch["product_digest"], dirty=bool(version["dirty"])),
                              submitted_by=body["uploaded_by"], results=[result for result, _ in prepared])
            number = self.insert_round(db, batch, version, round_body, kind="manual")
            return dict(ok=True, round=number, imported=len(prepared), skipped=skipped)

    def upload_cases(self, key, name, body):
        document = load_cases(body)
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM projects WHERE key=?", (key,)).fetchone():
                reject(404, "project-not-found", "project", "项目不存在")
            db.execute("INSERT OR IGNORE INTO batches(project,name,path,product_digest) VALUES (?,?,'','')", (key, name))
            batch = self.batch(db, key, name)
            previous = db.execute("SELECT * FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1", (batch["id"],)).fetchone()
            sha = hashlib.sha256(body["content"].encode()).hexdigest()
            current = {item["id"]: item for item in self.case_union(db, batch)}
            count = len(document["test_cases"])
            if previous and previous["sha256"] == sha:
                return dict(ok=True, upload=previous["number"], added=0, updated=0, unchanged=count,
                            union_count=len(current), unchanged_upload=True)
            number = previous["number"] + 1 if previous else 1
            added = sum(item["id"] not in current for item in document["test_cases"])
            updated = sum(item["id"] in current and item != current[item["id"]] for item in document["test_cases"])
            db.execute("INSERT INTO case_uploads(batch,number,filename,uploaded_by,uploaded_at,sha256,metadata,case_count,added,updated) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (batch["id"], number, body["filename"], body["uploaded_by"], now(),
                       sha, canonical(document["metadata"]), count, added, updated))
            for item in document["test_cases"]:
                db.execute("INSERT INTO case_versions(batch,case_id,upload,body) VALUES (?,?,?,?)",
                           (batch["id"], item["id"], number, canonical(item)))
            result = dict(ok=True, upload=number, added=added, updated=updated,
                          unchanged=count - added - updated, union_count=len(current) + added)
        if self.merge_callback:
            self.merge_callback(key, name, trigger="上传用例")
        return result

    def case_union(self, db, batch):
        return [json.loads(row["body"]) for row in db.execute(
            "SELECT v.body FROM case_versions v WHERE v.batch=? AND NOT EXISTS "
            "(SELECT 1 FROM case_versions newer WHERE newer.batch=v.batch AND newer.case_id=v.case_id AND newer.upload>v.upload) "
            "ORDER BY v.case_id", (batch["id"],))]

    def read_cases(self, key, name, include_state=False):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            uploads = [dict(upload=row["number"], filename=row["filename"], uploaded_by=row["uploaded_by"],
                            uploaded_at=row["uploaded_at"], case_count=row["case_count"], added=row["added"], updated=row["updated"])
                       for row in db.execute("SELECT * FROM case_uploads WHERE batch=? ORDER BY number", (batch["id"],))]
            cases = self.case_union(db, batch)
            scenarios = {row[0] for row in db.execute("SELECT scenario FROM scenarios WHERE batch=? AND current=1", (batch["id"],))}
            return dict(uploads=uploads, cases=cases, coverage=ratio(sum(item["id"] in scenarios for item in cases), len(cases)),
                        **(dict(etag=self.batch_etag(db, batch)) if include_state else {}))

    def cases_yaml(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            latest = db.execute("SELECT metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1", (batch["id"],)).fetchone()
            if not latest:
                reject(404, "cases-not-found", "cases", "尚无用例上传")
            document = dict(metadata=json.loads(latest["metadata"]), test_cases=self.case_union(db, batch))
            return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()

    def scenario_file(self, key, name):
        with self.store.connection() as db:
            batch = self.batch(db, key, name)
            version = self.latest_version(db, batch)
            if not version:
                reject(404, "scenario-file-not-found", "scenario-file", "尚无场景文件")
            return version["content"].encode()

    def insert_round(self, db, batch, version, body, kind="agent"):
        number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
        product = body["product"]
        cursor = db.execute("INSERT INTO rounds(batch,number,kind,version,product_commit,product_digest,product_dirty,"
                            "implementer,submitted_by,submitted_at,idempotency_key,body_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (batch["id"], number, kind, version["id"], product["commit"], product["digest"], product["dirty"],
                             body.get("implementer"), body["submitted_by"], now(), body.get("idempotency_key"),
                             hashlib.sha256(canonical(body).encode()).hexdigest()))
        round_id = cursor.lastrowid
        for row in body["results"]:
            implementer = body.get("implementer")
            fold = lambda name: " ".join(name.casefold().split())
            independence = ("undeclared" if implementer is None else
                            "self-check" if fold(implementer) == fold(row["executor"]) else "independent")
            cursor = db.execute("INSERT INTO results(round_id,scenario,fingerprint,status,actual,executed_at,executor,session,"
                                "finding,note,independence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (round_id, row["scenario"], row["fingerprint"], row["status"], row["actual"], row["executed_at"],
                                 row["executor"], row.get("session"), row.get("finding"), row.get("note"), independence))
            for ordinal, evidence in enumerate(row["evidence"], 1):
                db.execute("INSERT INTO result_evidence VALUES (?,?,?,?)", (cursor.lastrowid, ordinal, evidence["name"], evidence["sha256"]))
        return number

    def round(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            if isinstance(body, dict) and isinstance(body.get("idempotency_key"), str):
                existing = db.execute("SELECT * FROM rounds WHERE batch=? AND idempotency_key=?",
                                      (batch["id"], body["idempotency_key"])).fetchone()
                if existing:
                    if hashlib.sha256(canonical(body).encode()).hexdigest() != existing["body_sha256"]:
                        reject(409, "idempotency-conflict", "idempotency_key", "同一幂等键的正文不同")
                    summary = calculate(db, batch)
                    return dict(ok=True, round=existing["number"], statistics=summary["statistics"], queues=summary["queues"])
            validate_round(db, body)
            version = self.latest_version(db, batch)
            if body["scenario_file_sha256"] != version["sha256"]:
                reject(409, "scenario-file-stale", "scenario_file_sha256", "场景文件不是当前版本")
            self.check_scenarios(db, batch, body["results"])
            number = self.insert_round(db, batch, version, body)
            summary = calculate(db, batch)
            return dict(ok=True, round=number, statistics=summary["statistics"], queues=summary["queues"])

    def check_scenarios(self, db, batch, results):
        current = {row["scenario"]: row["fingerprint"] for row in db.execute(
            "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch["id"],))}
        problems = [dict(field=f"results[{index}].fingerprint", problem=f"场景 {row['scenario']} 不在当前版本或内容指纹已变更")
                    for index, row in enumerate(results) if current.get(row["scenario"]) != row["fingerprint"]]
        if problems:
            raise APIError(409, "scenario-stale", problems)

    def summary(self, db, batch):
        result = calculate(db, batch)
        result.pop("scenarios")
        version = self.latest_version(db, batch)
        return dict(ok=True, etag=self.batch_etag(db, batch), name=batch["name"], version=version["version"] if version else None,
                    pushed_at=version["pushed_at"] if version else None,
                    pushed_by=version["pushed_by"] if version else None,
                    dirty=bool(version["dirty"]) if version else None,
                    commit=version["commit_id"] if version else None,
                    writable=batch["project"] in self.writable_repos,
                    workspace_problems=self.workspace_problems.get((batch["project"], batch["name"]), []),
                    scenario_file_sha256=version["sha256"] if version else None, product_digest=batch["product_digest"], **result)

    def read_batch(self, key, name, scenarios=False):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            if scenarios:
                summary = calculate(db, batch)
                version = self.latest_version(db, batch)
                nodes = yaml.safe_load(version["content"])["sops"] if version else []
                return dict(ok=True, scenarios=summary["scenarios"], filters=summary["filters"], nodes=nodes,
                            has_version=version is not None, etag=self.batch_etag(db, batch))
            return self.summary(db, batch)

    def projects(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            projects = []
            for project in db.execute("SELECT * FROM projects ORDER BY key"):
                projects.append(dict(key=project["key"], id=project["identity"], name=project["name"],
                    defect_prefix=project["defect_prefix"], writable=project["key"] in self.writable_repos, batches=[self.summary(db, batch) for batch in db.execute(
                        "SELECT * FROM batches WHERE project=? ORDER BY name", (project["key"],))]))
            return dict(ok=True, projects=projects)

    def read_round(self, key, name, number):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            row = db.execute("SELECT * FROM rounds WHERE batch=? AND number=?", (batch["id"], number)).fetchone()
            if not row:
                reject(404, "round-not-found", "round", "轮次不存在")
            results = []
            for stored in db.execute("SELECT * FROM results WHERE round_id=? ORDER BY id", (row["id"],)):
                item = dict(stored)
                item["evidence"] = [dict(e) for e in db.execute(
                    "SELECT name,sha256 FROM result_evidence WHERE result_id=? ORDER BY ordinal", (stored["id"],))]
                for key_ in ("id", "round_id"):
                    item.pop(key_)
                for key_ in ("session", "finding", "note"):
                    if item[key_] is None:
                        item.pop(key_)
                results.append(item)
            version = db.execute("SELECT version FROM versions WHERE id=?", (row["version"],)).fetchone()[0]
            return dict(ok=True, round=row["number"], kind=row["kind"], version=version,
                        product=dict(commit=row["product_commit"], digest=row["product_digest"], dirty=bool(row["product_dirty"])),
                        implementer=row["implementer"], submitted_by=row["submitted_by"], submitted_at=row["submitted_at"],
                        results=results)

    def read_scenario(self, key, name, scenario):
        """详情保留当前与历史内容，不受 calculate 的当前版本过滤影响。"""
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            records = [dict(row) for row in db.execute(
                "SELECT * FROM scenarios WHERE batch=? AND scenario=? ORDER BY current DESC,id DESC",
                (batch["id"], scenario))]
            if not records:
                reject(404, "scenario-not-found", "scenario", "场景不存在")
            results = [dict(row) for row in db.execute(
                "SELECT r.*,t.number,t.kind,t.submitted_by,t.submitted_at FROM results r JOIN rounds t ON t.id=r.round_id "
                "WHERE t.batch=? AND r.scenario=? ORDER BY t.number,r.id", (batch["id"], scenario))]
            for row in results:
                row["evidence"] = [dict(item) for item in db.execute(
                    "SELECT r.name,r.sha256,e.name AS stored_name FROM result_evidence r JOIN evidence e ON e.sha256=r.sha256 "
                    "WHERE r.result_id=? ORDER BY r.ordinal", (row["id"],))]
                for evidence in row["evidence"]:
                    evidence["content_type"] = content_type(evidence.pop("stored_name"))
                    if is_text(evidence["content_type"]):
                        sha = evidence["sha256"]
                        with (self.store.data / "evidence" / sha[:2] / sha).open(encoding="utf-8", errors="replace") as source:
                            lines = list(islice(source, 201))
                            evidence["preview"] = "".join(lines[:200])
                            evidence["preview_truncated"] = len(lines) > 200
            groups = []
            for record in records:
                groups.append(dict(fingerprint=record["fingerprint"], current=bool(record["current"]),
                                   body=json.loads(record["body"]), results=[r for r in results if r["fingerprint"] == record["fingerprint"]]))
            latest = records[0]
            return dict(project=key, batch=name, etag=self.batch_etag(db, batch), scenario=scenario, node=latest["node"], title=latest["title"],
                        polarity=latest["polarity"], priority=latest["priority"], verify=latest["verify"],
                        fingerprint=latest["fingerprint"], current=bool(latest["current"]),
                        pushed_at=self.latest_version(db, batch)["pushed_at"], groups=groups,
                        summary=next((item for item in calculate(db, batch)["scenarios"] if item["scenario"] == scenario), None))

    def export_round(self, key, name, number):
        round_data = self.read_round(key, name, number)
        output = BytesIO()
        rows, ordinal = [], 0
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for result in round_data["results"]:
                row = {field: result[field] for field in ("scenario", "status", "actual", "executed_at", "executor")}
                row["evidence"] = []
                for evidence in result["evidence"]:
                    ordinal += 1
                    filename = evidence["name"].replace("\\", "/").rsplit("/", 1)[-1]
                    filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
                    if filename in ("", ".", ".."):
                        filename = "evidence.bin"
                    path = f"evidence/{ordinal}-{filename}"
                    raw, _ = self.evidence(evidence["sha256"])
                    archive.writestr(path, raw)
                    row["evidence"].append(path)
                for field in ("session", "finding", "note"):
                    if field in result:
                        row[field] = result[field]
                rows.append(canonical(row))
            archive.writestr("results.jsonl", "\n".join(rows) + "\n")
        return output.getvalue()

    def settings(self, key, body):
        p = Problems("project-invalid")
        prefix = body.get("defect_prefix") if isinstance(body, dict) else None
        if not isinstance(prefix, str) or not prefix.startswith("https://") or not prefix[len("https://"):].strip():
            p.add("defect_prefix", "缺陷链接前缀须以 https:// 开头")
        p.finish()
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM projects WHERE key=?", (key,)).fetchone():
                reject(404, "project-not-found", "project", "项目不存在")
            db.execute("UPDATE projects SET defect_prefix=? WHERE key=?", (prefix, key))
        return dict(ok=True, defect_prefix=prefix)

    def mark(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            validate_mark(body)
            current = db.execute("SELECT fingerprint FROM scenarios WHERE batch=? AND scenario=? AND current=1",
                                 (batch["id"], body["scenario"])).fetchone()
            if not current:
                reject(409, "scenario-stale", "scenario", f"场景 {body['scenario']} 不在当前版本")
            if current["fingerprint"] != body["fingerprint"]:
                reject(409, "scenario-stale", "fingerprint", "场景内容指纹已变更")
            if body["status"] in ("fail", "blocked"):
                prefix = db.execute("SELECT defect_prefix FROM projects WHERE key=?", (key,)).fetchone()[0]
                body = {**body, "defect_url": normalize(body["defect_url"], self.jira_url, prefix)}
            version = self.latest_version(db, batch)
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
            timestamp = now()
            observation = dict(marker=body["marker"], status=body["status"], recorded_at=timestamp)
            for field in ("defect_url", "note"):
                if body.get(field):
                    observation[field] = body[field]
            filename = f"observation-{number}.json"
            saved = self.store.save_evidence(db, canonical(observation).encode(), filename)
            result = dict(scenario=body["scenario"], fingerprint=body["fingerprint"], status=body["status"],
                          actual="人工复核通过", executor=body["marker"], executed_at=timestamp,
                          evidence=[dict(name=filename, sha256=saved["sha256"])])
            if body["status"] in ("fail", "blocked"):
                label = "失败" if body["status"] == "fail" else "受阻"
                result["finding"] = body["defect_url"]
                result["actual"] = f"人工复核不通过（{label}），缺陷 {body['defect_url']}"
            if "note" in body:
                result["note"] = body["note"]
            round_body = dict(product=dict(commit=version["commit_id"], digest=batch["product_digest"], dirty=bool(version["dirty"])),
                              submitted_by=body["marker"], results=[result])
            number = self.insert_round(db, batch, version, round_body, kind="manual")
            summary = calculate(db, batch)
            scenario = next(item for item in summary["scenarios"] if item["scenario"] == body["scenario"])
            return dict(ok=True, round=number, scenario=scenario, filters=summary["filters"])

    def push(self, key, batch, body, *, preserve_project_name=False):
        validate_push(body)
        document = load_scenarios(body["content"])
        if document["metadata"]["batch"] != batch:
            reject(422, "project-mismatch", "metadata.batch", "批次与地址不一致")
        if project_key(body["project"]["id"]) != key:
            reject(422, "project-mismatch", "project.id", "项目身份与地址不一致")
        scenarios = [(node["id"], scenario) for node in document["sops"]
                     for scenario in node["scenarios"]]
        computed = {s["id"]: fingerprint(s) for _, s in scenarios}
        supplied = body["fingerprints"]
        mismatches = [dict(field=f"fingerprints.{s}", problem=f"场景 {s} 的内容指纹不一致")
                      for s in sorted(computed.keys() | supplied.keys()) if computed.get(s) != supplied.get(s)]
        if mismatches:
            raise APIError(409, "fingerprint-mismatch", mismatches)
        with self.store.transaction() as db:
            identity = body["project"]["id"]
            prefix = (f"https://{identity}/issues/" if
                      re.fullmatch(r"github\.com/[^/]+/[^/]+", identity, re.I) else None)
            conflict = "DO NOTHING" if preserve_project_name else "DO UPDATE SET name=excluded.name"
            db.execute("INSERT INTO projects VALUES (?,?,?,?) ON CONFLICT(key) " + conflict,
                       (key, identity, body["project"]["name"], prefix))
            db.execute("INSERT INTO batches(project,name,path,product_digest) VALUES (?,?,?,?) "
                       "ON CONFLICT(project,name) DO UPDATE SET path=excluded.path, product_digest=excluded.product_digest",
                       (key, batch, body["path"], body["product_digest"]))
            batch_id = db.execute("SELECT id FROM batches WHERE project=? AND name=?", (key, batch)).fetchone()[0]
            for source in body.get('sop_sources', []):
                last = db.execute('SELECT version,content FROM sop_versions WHERE batch=? AND path=? ORDER BY version DESC LIMIT 1',
                                  (batch_id, source['path'])).fetchone()
                if not last or last['content'] != source['content']:
                    db.execute('INSERT INTO sop_versions(batch,path,version,ref,commit_id,sha256,confirmed_by,content,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)',
                               (batch_id, source['path'], last['version'] + 1 if last else 1, source['ref'], source['commit'],
                                source['sha256'], source['confirmed_by'], source['content'], now()))
            if isinstance(body.get("entries"), dict):
                db.execute("INSERT INTO batch_entries(batch,content) VALUES (?,?) ON CONFLICT(batch) DO UPDATE SET content=excluded.content",
                           (batch_id, canonical(body["entries"])))
            previous = db.execute("SELECT * FROM versions WHERE batch=? ORDER BY version DESC LIMIT 1", (batch_id,)).fetchone()
            if previous and previous["content"] == body["content"]:
                return dict(ok=True, version=previous["version"], added=[], changed=[], removed=[], unchanged=True)
            version = previous["version"] + 1 if previous else 1
            old = {row["scenario"]: row["fingerprint"] for row in db.execute(
                "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch_id,))}
            added = [s for s in computed if s not in old]
            changed = [s for s in computed if s in old and computed[s] != old[s]]
            removed = [s for s in old if s not in computed]
            db.execute("INSERT INTO versions(batch,version,content,sha256,path,commit_id,dirty,pushed_by,pushed_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (batch_id, version, body["content"],
                       hashlib.sha256(body["content"].encode()).hexdigest(), body["path"],
                       body["commit"], body["dirty"], body["pushed_by"], now()))
            db.execute("UPDATE scenarios SET current=0 WHERE batch=?", (batch_id,))
            for node, scenario in scenarios:
                db.execute("INSERT INTO scenarios(batch,scenario,fingerprint,node,title,polarity,priority,verify,regression,body,current) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(batch,scenario,fingerprint) DO UPDATE SET "
                           "node=excluded.node,title=excluded.title,polarity=excluded.polarity,priority=excluded.priority,"
                           "verify=excluded.verify,regression=excluded.regression,body=excluded.body,current=1",
                           (batch_id, scenario["id"], fingerprint(scenario), node,
                           scenario["title"], scenario["polarity"], scenario["priority"], scenario["verify"],
                           canonical(scenario.get("regression")), canonical(scenario)))
        if self.merge_callback and not body['pushed_by'].startswith('看板合并 #'):
            self.merge_callback(key, batch, trigger='场景文件变化')
        return dict(ok=True, version=version, added=added, changed=changed, removed=removed, unchanged=False)
