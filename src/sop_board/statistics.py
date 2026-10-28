"""批次队列和统计的唯一计算入口，供接口与页面复用。"""

import json

from .time_format import local_time
from .merge_state import read_holds, held_scenarios

ORIGIN_LABELS = dict(board='看板', session='会话', unknown='历史记录未注明')
INDEPENDENCE_LABELS = {'independent': '独立验收', 'self-check': '实现者自检', 'undeclared': '未声明', 'mixed': '混合记录'}


def independence_counts(rows):
    result = {key: 0 for key in ('independent', 'self-check', 'undeclared')}
    for row in rows:
        key = row.get('independence', 'undeclared')
        result[key if key in result else 'undeclared'] += 1
    return result


def round_identity(rows):
    counts = independence_counts(rows)
    used = [key for key, count in counts.items() if count]
    return dict(executors=list(dict.fromkeys(row['executor'] for row in rows)),
                independence=used[0] if len(used) == 1 else 'mixed' if used else 'undeclared',
                independence_counts=counts)


def ratio(numerator, denominator):
    return dict(numerator=numerator, denominator=denominator,
                rate=numerator / denominator if denominator else None)


def stale(agent, product_digest):
    """通过记录不能直接代表该产品：内容摘要不同，或执行期间产品有变化（执行时的中间状态无法还原，须判定影响）。"""
    return agent['product_digest'] != product_digest or bool(agent['product_changed'])


def pending_scope(db, batch, summary, product_digest):
    """按钮计数与手动入队共用；失败也须重跑，不改变展示队列的口径。"""
    scope = []
    for scene in summary['scenarios']:
        if scene.get('held'):
            continue
        agent = scene['latest_agent']
        latest = max((r for r in (agent, scene['latest_manual']) if r),
                     key=lambda r: r['number'], default=None)
        reason = None
        if latest is None:
            prior = db.execute('SELECT 1 FROM scenarios WHERE batch=? AND scenario=? AND fingerprint!=? LIMIT 1',
                               (batch['id'], scene['scenario'], scene['fingerprint'])).fetchone()
            reason = 'changed' if prior else 'new'
        elif latest['status'] == 'fail':
            reason = 'failed'
        elif agent and agent['status'] == 'pass' and latest['status'] == 'pass' and stale(agent, product_digest):
            reason = 'source-changed'
        if reason:
            scope.append(dict(scenario=scene['scenario'], reason=reason))
    return scope


def reused(row):
    return (row["note"] or "").startswith("沿用：")


def rerun(row):
    # 重跑指实际执行并得出通过或失败的场景；受阻、跳过既不是沿用也不是重跑。
    return not reused(row) and row["status"] in ("pass", "fail")


def round_counts(db, batch_id, numbers):
    """按任务登记的轮次从正式结果计算沿用数与重跑数；看板、会话与旧任务用同一来源。"""
    numbers = [int(n) for n in numbers]
    rows = db.execute("SELECT r.status,r.note FROM results r JOIN rounds t ON t.id=r.round_id WHERE t.batch=? AND t.kind='agent' "
                      "AND t.number IN (" + ",".join("?" * len(numbers)) + ")", (batch_id, *numbers)).fetchall() if numbers else []
    return dict(reused_count=sum(reused(r) for r in rows), rerun_count=sum(rerun(r) for r in rows))


def task_counts(db, workspace, batch, rounds):
    """执行任务的轮次是整数（本批次）或 {batch, round}（会话修复复验跨批次）；按批次分组后汇总。"""
    grouped = {}
    for item in rounds:
        name, number = (item["batch"], item["round"]) if isinstance(item, dict) else (batch, item)
        grouped.setdefault(name, []).append(number)
    counts = dict(reused_count=0, rerun_count=0)
    for name, numbers in grouped.items():
        row = db.execute("SELECT id FROM batches WHERE workspace=? AND name=?", (workspace, name)).fetchone()
        if row:
            for key, value in round_counts(db, row["id"], numbers).items():
                counts[key] += value
    return counts


def calculate(db, batch):
    held = held_scenarios(read_holds(db, batch))
    scenarios = [dict(row) for row in db.execute(
        "SELECT * FROM scenarios WHERE batch=? AND current=1 ORDER BY id", (batch["id"],))]
    rows = [dict(row) for row in db.execute(
        "SELECT r.*,t.number,t.kind,t.product_digest,t.submitted_at,t.implementer,t.submitted_by,COALESCE(o.origin,'unknown') AS execution_origin,"
        "COALESCE(c.changed_json,'[]') AS product_changed "
        "FROM results r JOIN rounds t ON t.id=r.round_id LEFT JOIN round_origins o ON o.round_id=t.id "
        "LEFT JOIN round_product_changes c ON c.round_id=t.id "
        "WHERE t.batch=? ORDER BY t.number,r.id", (batch["id"],))]
    by_scenario = {}
    evidence = {}
    for item in db.execute("SELECT e.* FROM result_evidence e JOIN results r ON r.id=e.result_id "
                           "JOIN rounds t ON t.id=r.round_id WHERE t.batch=? ORDER BY e.ordinal", (batch["id"],)):
        evidence.setdefault(item["result_id"], []).append(dict(name=item["name"], sha256=item["sha256"]))
    for row in rows:
        row["round"] = row["number"]
        row["product_changed"] = json.loads(row["product_changed"])
        row["reused"] = row["kind"] == "agent" and reused(row)
        row["rerun"] = row["kind"] == "agent" and rerun(row)
        row["executed_at_display"] = local_time(row["executed_at"])
        row["submitted_at_display"] = local_time(row["submitted_at"])
        row["evidence"] = evidence.get(row["id"], [])
        by_scenario.setdefault((row["scenario"], row["fingerprint"]), []).append(row)
    queues = dict(pending=[], issues=[], retest=[])
    agent_pass = agent_fail = first_pass = first_fail = reviewed = manual_pass = manual_fail = executed = 0
    details, misjudgments = [], []
    agent_counts = {"pass": 0, "fail": 0, "blocked": 0, "skip": 0}
    filters = {"all": len(scenarios), "unreviewed": 0, "agent-pass": 0, "fail": 0, "blocked": 0}
    for scenario in scenarios:
        history = by_scenario.get((scenario["scenario"], scenario["fingerprint"]), [])
        agents = [r for r in history if r["kind"] == "agent"]
        manuals = [r for r in history if r["kind"] == "manual"]
        a, m, latest = agents[-1] if agents else None, manuals[-1] if manuals else None, history[-1] if history else None
        filters["unreviewed"] += m is None
        filters["agent-pass"] += bool(a and a["status"] == "pass")
        if latest and latest["status"] in ("fail", "blocked"):
            filters[latest["status"]] += 1
        eligible = scenario['scenario'] not in held
        executed += bool(history) and eligible
        if agents and eligible:
            agent_counts[a["status"]] += 1
            agent_pass += a["status"] == "pass"
            agent_fail += a["status"] == "fail"
            first_pass += agents[0]["status"] == "pass"
            first_fail += agents[0]["status"] == "fail"
        if eligible and a and a["status"] == "pass" and m and m["number"] > a["number"]:
            reviewed += 1
            manual_pass += m["status"] == "pass"
            manual_fail += m["status"] == "fail"
        if not latest or (a and latest["status"] == "pass" and a["status"] == "pass" and
                          stale(a, batch["product_digest"])):
            queue = "pending"
        elif latest["status"] in ("fail", "blocked", "skip"):
            queue = "issues"
        elif a and latest is a and a["status"] == "pass":
            queue = "retest"
        else:
            queue = "reviewed"
        if not eligible:
            queue = 'held'
        if queue in queues:
            queues[queue].append(scenario["scenario"])
        preceding_agent = None
        for row in history:
            if row["kind"] == "agent":
                preceding_agent = row
            elif row["status"] == "fail" and preceding_agent and preceding_agent["status"] == "pass":
                misjudgments.append(dict(scenario=scenario["scenario"], round=row["number"],
                    defect_url=row["finding"], agent_executor=preceding_agent["executor"], marker=row["executor"]))
        details.append(dict(scenario=scenario["scenario"], node=scenario["node"], title=scenario["title"],
                            polarity=scenario["polarity"], priority=scenario["priority"], verify=scenario["verify"],
                            fingerprint=scenario["fingerprint"], status=latest["status"] if latest else None,
                            queue=queue, held=not eligible, latest_agent=a, latest_manual=m,
                            body=json.loads(scenario["body"])))
    execution_count = sum(s['scenario'] not in held for s in scenarios)
    statistics = dict(agent_pass_rate=ratio(agent_pass, agent_pass + agent_fail),
                      manual_pass_rate=ratio(manual_pass, manual_pass + manual_fail),
                      retest_coverage=ratio(reviewed, agent_pass), first_pass_rate=ratio(first_pass, first_pass + first_fail),
                      executed_progress=ratio(executed, execution_count), reviewed_progress=ratio(reviewed, agent_pass))
    statistics['independence_counts'] = independence_counts([s['latest_agent'] for s in details if not s['held'] and s['latest_agent']])
    rounds = []
    for row in db.execute("SELECT t.*,v.version AS scenario_version,COALESCE(o.origin,'unknown') AS execution_origin,"
                          "COALESCE(c.changed_json,'[]') AS product_changed FROM rounds t JOIN versions v ON v.id=t.version "
                          "LEFT JOIN round_origins o ON o.round_id=t.id LEFT JOIN round_product_changes c ON c.round_id=t.id "
                          "WHERE t.batch=? ORDER BY number", (batch["id"],)):
        item = dict(row)
        item["version"] = item.pop("scenario_version")
        item["product_changed"] = json.loads(item["product_changed"])
        results = [r for r in rows if r["round_id"] == item["id"]]
        item.update(round_identity(results))
        item["pass_rate"] = ratio(sum(r["status"] == "pass" for r in results), len(results))
        item["reused_count"] = sum(r["reused"] for r in results)
        item["rerun_count"] = sum(r["rerun"] for r in results)
        item["reused_scenarios"] = [dict(scenario=r["scenario"], reason=r["note"][3:].strip()) for r in results if r["reused"]]
        rounds.append(item)
    latest_agent = next((r for r in reversed(rounds) if r["kind"] == "agent"), None)
    return dict(reused_count=sum(bool(s["latest_agent"] and s["latest_agent"]["reused"]) for s in details),
                rerun_count=sum(bool(s["latest_agent"] and s["latest_agent"]["rerun"]) for s in details), statistics=statistics, queues=queues, queue_counts={k: len(v) for k, v in queues.items()},
                agent_execution=dict(counts=agent_counts, submitted_at=latest_agent["submitted_at"] if latest_agent else None),
                progress=dict(executed=statistics["executed_progress"], reviewed=statistics["reviewed_progress"]),
                scenario_count=len(scenarios), execution_count=execution_count, scenarios=details, rounds=rounds, misjudgments=misjudgments, filters=filters)
