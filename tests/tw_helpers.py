"""Prepare a disposable tw project without committing or changing the working repo."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

from conftest import ROOT, TEST_ENV
from helpers import IDS


def tw_command():
    installed = shutil.which("tw")
    if installed:
        return [installed]
    script = Path.home() / ".agents/skills/team-knowledge/scripts/tw.py"
    return [sys.executable, str(script)] if script.is_file() else None


def prepare_tw_project(directory, scenario_content, command):
    project = directory / "tw-project"
    # A local clone supplies an existing HEAD. No commit, checkout, or branch change is used.
    subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout", str(ROOT), str(project)],
                   check=True, capture_output=True, text=True, timeout=20, env=TEST_ENV)
    (project / "README.md").write_text("# Export contract fixture\nTemporary isolated project for exported result compatibility.\n")
    log = directory / "tw-commands.log"

    def run(*args):
        argv = [*command, *args, "--root", str(project)]
        result = subprocess.run(argv, cwd=project, env={**TEST_ENV, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=30)
        with log.open("a") as output:
            output.write("$ " + " ".join(argv) + "\n" + result.stdout + result.stderr + "\n")
        assert result.returncode == 0, log.read_text()
        return result.stdout

    run("init", "--apply")
    config_path = project / ".tw/config.json"
    config = json.loads(config_path.read_text())
    config.update(project={"id": "local/export-contract", "name": "Export contract fixture"}, context=["README.md"],
        checks={"unit": {"argv": [sys.executable, "-c", "print('fixture')"], "purpose": "Isolated contract fixture",
            "cwd": ".", "timeout": 30, "format": "command",
            "required_cases": [], "required_env": [], "environment": {"kind": "stateless"},
            "coverage": {"level": "interface", "entrypoints": ["export"], "environment": "temporary-project",
                         "substitutes": [], "isolation": "isolated", "isolation_evidence": []}}},
        required_checks={"code": ["unit"], "docs": []}, risk_checks={}, accounts=[], inputs=[], external_inputs={},
        environment_versions={"python": [sys.executable, "--version"]},
        acceptance_entries={"自定义入口": {"surface": "cli", "operate": "Read exported result file",
            "observe": ["Result fields and evidence"], "evidence": ["Exported ZIP"], "isolation": "Temporary data directory"}})
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    (project / "demo.yaml").write_text(scenario_content)
    (project / "design.md").write_text('''---
design: "D-export-contract"
task: "EXPORT-CONTRACT"
revision: 1
assessment: {"complexity":"simple","risks":[],"effort":"test fixture","drivers":["export compatibility"],"uncertainties":[],"required_skills":[]}
roles: {"design_owner":"Fixture Owner","implementation_owner":"Fixture Implementer","acceptance_owner":"Fixture Tester"}
---

## Scope
<!-- tw-section id=scope-global type=global-scope -->
Verify the result file contract in an isolated fixture.

- 需求级验收：A1

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | export | read result file | board | tw | fixture | A1 |

## Acceptance
<!-- tw-section id=A1 type=acceptance -->
Accept exported result fields and observation evidence.

- 场景：demo.yaml#N1、N2
- 依据：位置 SOP-BOARD-2 I2；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## Task
<!-- tw-section id=T1 type=task -->
Verify result compatibility.

- 验收：A1
- 范围：无
- 前置任务：无
''')
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    task = dict(data_version=1, id="EXPORT-CONTRACT", base=base, goal="Verify result compatibility",
                non_goals=[], kind="code", risks=[], acceptance=[{"id": "A1"}], design_id="D-export-contract",
                section_ids=["T1"], paths=[], symbols=[], scopes=[], acceptance_scope="requirement")
    (project / "task.json").write_text(json.dumps(task))
    run("team", "start", "--task", "task.json", "--design", "design.md")
    return run, log
