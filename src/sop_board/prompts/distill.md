# 沉淀 #$work：把独立通过的场景写成守护测试

工作目录：$directory（检出 $checkout）

按 regression-distill 技能执行：$skill。材料：

- scenarios.yaml：要沉淀的场景原文。测试断言只取自这里的 expected 与 forbidden_effects。
- acceptance.json：每个场景独立真实入口通过的结果编号与运行目录。Web 场景的通过 spec 就在其运行目录的 scenarios/ 下。
- repos.json：本检出各仓库的目录。测试写在这些仓库里，留作未提交改动，由开发者审核提交。
- 守护索引：$index（只读，submit 负责合并）。

## 要求

1. 每条 expected 与 forbidden_effects 都要有测试覆盖，选能观察到该结果的最低成本层（unit / api / component / browser）。只覆盖部分预期的场景不要写进 guard.json。起点要求某个依赖不可用的场景，测试写在单元、接口或组件层，用替身模拟依赖不可用，不停真实服务、不运行 `sop-board env stop`；写不出这样的测试时不写进 guard.json，在交回时写明原因。
2. 只改测试、夹具与测试启动脚本，不改产品代码与场景文件。测试运行时的输出（报告、截图、缓存）写到仓库外或已被忽略的目录：retest 要求运行前后产品快照不变。
3. 本机实际运行，确认声明的测试名出现在执行清单中且通过。再做一次检出能力核对：在独立临时副本里受控改错，或复现已知缺陷，确认失败的是对应业务断言。把改错的差异、命令与失败输出存进一个文件，作为 detection 证据。
4. 在工作目录写 guard.json：

```json
{
  "commands": {"unit": {"repo": "app", "cwd": ".", "argv": ["uv", "run", "pytest", "-q", "--junitxml={junit}"], "timeout": 900}},
  "guards": {
    "<文件>#<场景编号>": {
      "acceptance": {"result": <acceptance.json 中的结果编号>},
      "tests": [{"id": "t1", "repo": "app", "path": "tests/test_x.py", "name": "tests/test_x.py::test_y", "layer": "unit", "command": "unit"}],
      "coverage": {"expected": [{"index": 0, "text": "<原文>", "tests": ["t1"]}], "forbidden_effects": [{"index": 0, "text": "<原文>", "tests": ["t1"]}]},
      "detection": {"method": "mutation", "assertion": "<改错后失败的断言>", "evidence": "detection-<场景>.txt"}
    }
  }
}
```

- 命令只用 argv，不经 shell；`{junit}` 由运行器替换为 junit 输出路径，`{playwright}` 替换为看板运行时的 Playwright CLI。命令必须输出 junit。命令（`cwd`、`argv`、`env`）与测试文件随仓库提交，路径都写仓库内相对路径，不写本机绝对路径。
- 浏览器层测试的命令写 `"browser": "<身份>"`，由看板提供 PW_CDP_ENDPOINT；测试不自己启动浏览器。以运行目录里那份独立通过的 spec 为起点，连同 library.mjs 及其导入放进仓库 spec 库的任意子目录，spec 可改名；另配连接 PW_CDP_ENDPOINT 的守护 fixtures.mjs 与配置，不依赖原运行的 scope.json 等材料（见技能 references/report.md 的 commands）。原 spec 是首次保真基线，比较时排除运行适配接口、保留业务断言与项目辅助代码；改写已有 spec 的断言时，在该守护的 ledger 里逐条写 `{"assertion_id", "kind": "rewrite", "reason"}`。

5. 运行 `sop-board distill submit`。当前状态须为通过；状态不是通过时返回 `distill-not-passing` 并写明当前状态，回到通过后再登记。它会核对 acceptance、覆盖与检出能力证据，实际运行一次命令，全部合格后才写进守护索引。被拒时按输出修正后再提交；输出指出问题在保真基线（独立通过的那份 spec）时停下，按技能第 5 步交回。不要提交 git。
