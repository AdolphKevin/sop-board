# regression.json

UTF-8 JSON，放在仓库外。下列字段是 `sop-board distill submit` 的登记契约，格式版本为 `1`。报告描述原始证据，不能代替证据。

## 字段与核对

- 顶层：`schema_version`、实际登记人 `registered_by`、任务或开发会话标识 `session`、非空 `scenarios` 数组。
- 每条场景：`batch`、`scenario_file`（如 `app:docs/sop/refund.yaml`，须与工作区配置一致）、`scenario`、`fingerprint`、`confirmed_by`（确认人数组）、`independent_acceptance`（`completed` 或 `incomplete`）、`pass_evidence`、`coverage`、`tests`、`local_run`、`detection`、`ci`；可选 `findings` 记录失败现象、依据与疑似原因。
- `fingerprint` 使用 Flow Board 对 `initial_state`、`steps`、`expected`、`forbidden_effects` 的现行算法，可调用 `sop_board.service.fingerprint(scene)`；不是整份 YAML 的摘要。`pass_evidence` 含 `kind`（`self-check` 或 `acceptance`）、`record`（轮次或自检原始记录位置）、`scenario_fingerprint`、`product_commit`（多仓可写 `别名:提交` 清单）、`product_digest`、`executor`。这些对应沉淀开始前核对的真实通过版本。
- `coverage.expected` 和 `coverage.forbidden_effects` 按原文顺序逐项列全。每项有从零开始的 `index`、原文 `text`、覆盖测试的 `tests`（不重复的测试 `id` 数组）、仍需真实入口核对的 `remaining`。一条复合预期可列多个测试，分别覆盖不同断言，同一断言仍只覆盖一次。完整覆盖时 `remaining` 可为空，未覆盖或部分覆盖时写明范围和观察方式；禁止把整条未测预期略去。即使全部被测试覆盖，独立业务验收仍按标准执行。
- 每个测试：本场景内唯一 `id`、仓库别名 `repo`、规范化 origin 身份 `identity`（主机/仓库路径，无协议和 `.git`）、实际仓库根目录 `root`、仓内相对文件 `path`、文件字节的 SHA-256 `sha256`、执行清单中的精确 `name`、`layer`（`unit`、`api-db`、`component`、`browser`）、`command`、`commit`（未提交为 `null`，已提交为完整提交号）。测试名或可选 `tag` 须含 `<批次>#<场景编号>`。工作树或独立副本的 origin 身份须与工作区登记仓库一致。
- `local_run`：`status` 为 `passed`、`failed`、`not-run`，`output` 为原始输出位置，`executed_tests` 为实际执行清单（每项 `name`、`status: pass/fail/skip`，可选 `test` 引用声明测试的 id），`reason` 为结果解释。不同仓库或文件存在同名测试时，用 `test` 区分，仍保留实际名称。逐个声明测试核对名称；缺失、跳过、零用例或环境缺失记 `not-run`，实际业务断言失败记 `failed`，全部实际通过才记 `passed`。
- `detection`：`status` 为 `verified` 或 `not-run`，`method` 为 `known-defect`、`mutation` 或 `none`，`record` 为缺陷版本/临时副本差异及原始输出位置，`assertion` 为实际失败的业务断言，`reason` 说明选择或未运行原因。
- `ci`：`status` 为 `not-integrated`（未接入）、`unverified`（已接入，未验证）或 `ran`（已运行）；`job`、`command` 指产品 CI 任务与命令，`run` 为运行编号或链接，`commit` 为对应提交，`executed_tests` 与本机清单格式相同，`output` 为原始输出位置。后四项在未运行时可为空；`ran` 必须有运行、提交、输出且全部声明测试实际执行（可失败，不能跳过）。

没有合适测试层而只保留真实入口验收的场景，也在报告列全：`tests` 为空、所有覆盖项的 `remaining` 非空、本机为 `not-run`，不声明 CI 已运行。登记核对该场景后不为它创建测试关联，其他场景的有效关联照常登记。

登记先核对整份报告：当前场景与指纹、逐项覆盖、仓库 origin、实际文件与摘要。`commit: null` 读取工作区文件；已提交读取指定提交中的文件，不以当前工作区文件替代。全部合格才写关联，任意不符整份拒绝。同一场景指纹、仓库、测试文件与测试名再次登记更新原关联；提交后改填 `commit` 再登记即从“待提交”变为“已提交”。仍为 `null` 时，若实际 HEAD 提交中的文件与报告摘要一致，也会在核对后记录该完整提交号。场景指纹变化使旧关联显示“已过期，需重新沉淀”。登记不产生场景轮次或修改验收结论。

## 完整示例

以下摘要、提交与路径为演示占位值，登记前须从实际文件和证据取得。该场景有两条预期、一条禁止副作用，其中通知仍需真实入口核对。

```json
{
  "schema_version": 1,
  "registered_by": "开发者甲",
  "session": "refund-distill-session-1",
  "scenarios": [
    {
      "batch": "refund",
      "scenario_file": "app:docs/sop/refund.yaml",
      "scenario": "R1-P01",
      "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "confirmed_by": ["业务负责人甲"],
      "independent_acceptance": "incomplete",
      "pass_evidence": {
        "kind": "self-check",
        "record": "/tmp/refund-evidence/self-check.json",
        "scenario_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "product_commit": "app:1111111111111111111111111111111111111111",
        "product_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "executor": "开发者甲"
      },
      "coverage": {
        "expected": [
          {"index": 0, "text": "退款后订单状态为已退款", "tests": ["refund-once"], "remaining": ""},
          {"index": 1, "text": "买家收到退款通知", "tests": [], "remaining": "经真实消息接收端核对买家收到一次通知"}
        ],
        "forbidden_effects": [
          {"index": 0, "text": "同一订单重复退款", "tests": ["refund-once"], "remaining": ""}
        ]
      },
      "tests": [
        {
          "id": "refund-once",
          "repo": "app",
          "identity": "example.invalid/demo/shop",
          "root": "/tmp/shop",
          "path": "tests/test_refund.py",
          "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
          "name": "test_refund_once",
          "tag": "refund#R1-P01",
          "layer": "api-db",
          "command": "pytest tests/test_refund.py -v",
          "commit": null
        }
      ],
      "local_run": {
        "status": "passed",
        "output": "/tmp/refund-evidence/local-run.txt",
        "executed_tests": [{"name": "test_refund_once", "status": "pass"}],
        "reason": "声明的测试均在实际执行清单中且通过"
      },
      "detection": {
        "status": "verified",
        "method": "known-defect",
        "record": "/tmp/refund-evidence/bug-refund-twice.txt",
        "assertion": "第二次退款后退款记录总数仍为 1",
        "reason": "已知缺陷版本在该业务断言失败，环境与依赖正常"
      },
      "ci": {
        "status": "unverified",
        "job": ".github/workflows/tests.yml:refund",
        "command": "pytest tests/test_refund.py -v",
        "run": "",
        "commit": "",
        "executed_tests": [],
        "output": ""
      },
      "findings": ["独立验收未完成；CI 已接入，未验证"]
    }
  ]
}
```
