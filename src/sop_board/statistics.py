"""批次队列和统计的唯一计算入口，供接口与页面复用。"""

import json

from .time_format import local_time


def ratio(numerator, denominator):
    return dict(numerator=numerator, denominator=denominator,
                rate=numerator / denominator if denominator else None)


def calculate(db, batch):
    scenarios = [dict(row) for row in db.execute(
        "SELECT * FROM scenarios WHERE batch=? AND current=1 ORDER BY id", (batch["id"],))]
    rows = [dict(row) for row in db.execute(
        "SELECT r.*,t.number,t.kind,t.product_digest,t.submitted_at FROM results r JOIN rounds t ON t.id=r.round_id "
        "WHERE t.batch=? ORDER BY t.number,r.id", (batch["id"],))]
    by_scenario = {}
    evidence = {}
    for item in db.execute("SELECT e.* FROM result_evidence e JOIN results r ON r.id=e.result_id "
                           "JOIN rounds t ON t.id=r.round_id WHERE t.batch=? ORDER BY e.ordinal", (batch["id"],)):
        evidence.setdefault(item["result_id"], []).append(dict(name=item["name"], sha256=item["sha256"]))
    for row in rows:
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
        executed += bool(history)
        if agents:
            agent_counts[a["status"]] += 1
            agent_pass += a["status"] == "pass"
            agent_fail += a["status"] == "fail"
            first_pass += agents[0]["status"] == "pass"
            first_fail += agents[0]["status"] == "fail"
        if a and a["status"] == "pass" and m and m["number"] > a["number"]:
            reviewed += 1
            manual_pass += m["status"] == "pass"
            manual_fail += m["status"] == "fail"
        regression = json.loads(scenario["regression"])
        if not latest or (a and latest is a and a["status"] == "pass" and
                          a["product_digest"] != batch["product_digest"] and not (regression or {}).get("check")):
            queue = "pending"
        elif latest["status"] in ("fail", "blocked", "skip"):
            queue = "issues"
        elif a and latest is a and a["status"] == "pass":
            queue = "retest"
        else:
            queue = "reviewed"
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
                            queue=queue, latest_agent=a, latest_manual=m,
                            regression=regression, body=json.loads(scenario["body"])))
    statistics = dict(agent_pass_rate=ratio(agent_pass, agent_pass + agent_fail),
                      manual_pass_rate=ratio(manual_pass, manual_pass + manual_fail),
                      retest_coverage=ratio(reviewed, agent_pass), first_pass_rate=ratio(first_pass, first_pass + first_fail),
                      executed_progress=ratio(executed, len(scenarios)), reviewed_progress=ratio(reviewed, agent_pass))
    rounds = []
    for row in db.execute("SELECT t.*,v.version AS scenario_version FROM rounds t JOIN versions v ON v.id=t.version "
                          "WHERE t.batch=? ORDER BY number", (batch["id"],)):
        item = dict(row)
        item["version"] = item.pop("scenario_version")
        results = [r for r in rows if r["round_id"] == item["id"]]
        item["pass_rate"] = ratio(sum(r["status"] == "pass" for r in results), len(results))
        rounds.append(item)
    latest_agent = next((r for r in reversed(rounds) if r["kind"] == "agent"), None)
    return dict(statistics=statistics, queues=queues, queue_counts={k: len(v) for k, v in queues.items()},
                agent_execution=dict(counts=agent_counts, submitted_at=latest_agent["submitted_at"] if latest_agent else None),
                progress=dict(executed=statistics["executed_progress"], reviewed=statistics["reviewed_progress"]),
                scenario_count=len(scenarios), scenarios=details, rounds=rounds, misjudgments=misjudgments, filters=filters)
