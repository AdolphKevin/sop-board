# 验收契约

看板的校验器是字段约束的事实来源；本文件只说明执行者要产出与读取的内容。

## 场景

`scenarios.yaml` 是本轮场景的列表，每项含 `key`（`<文件>#<编号>`）、`reason`、`id`、`polarity`、`title`、`actor`、`priority`、`verify`、`initial_state`、`steps`、`expected`、`forbidden_effects`，反向场景另有 `negative_type`。业务预期由上游确认，浏览器驱动不改写。本轮文件可能只含待执行的一部分，不为补结构新造场景。

## 结果

`results.jsonl` 每行一个场景，写在运行目录：

| 字段 | 内容 |
| --- | --- |
| `scenario` | 场景 key |
| `status` | `pass`、`fail`、`blocked`、`skip`、`not-executed` |
| `actual` | 实际观察到的结果 |
| `executed_at` | 带时区的实际执行时间 |
| `executor` | 执行者（`convert` 写入） |
| `evidence` | `evidence/` 下的相对路径数组，单个文件不超过 64 MiB |
| `finding`、`note` | blocked、skip、not-executed 须写 `finding` |

实际执行的 pass、fail 至少一份真实证据；`not-executed` 不附证据，留在待执行范围。`convert` 生成上述字段，其他入口的行由对应驱动写入后合并。

## 场景标签

`tags(scenarioId)` 只返回完整场景键标签，例如 `['@shop#W']`，不表示验收通过。

## 回归

场景是验收单位，场景执行会话不写测试。使用者另行要求沉淀时，由随包 [regression-distill](../../regression-distill/SKILL.md) 把独立通过的场景写成守护测试，经 `sop-board distill submit` 登记；之后 `sop-board retest` 运行守护，未通过的场景转待复测。本轮通过的 spec 是浏览器层沉淀的保真基线，所以按 [spec 的导入](web.md#spec-的导入)规则写。
