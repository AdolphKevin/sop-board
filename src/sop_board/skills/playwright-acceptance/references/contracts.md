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

## 配对标签

`pairs.json` 顶层为 `covered`、`conflict`、`disposed` 数组。`covered`、`conflict` 含 `case`、`scenarios`、`note`；`disposed` 含 `case`、`state`、`reason`、`confirmed_by`。`sop-board playwright tags` 只把已确定的对应关系转成 Playwright tag，不生成业务预期，也不把标签当作通过结果。

## 回归

场景是验收单位。由人触发的测试建设使用随包 [regression-distill](../../regression-distill/SKILL.md)，由 `sop-board distill submit` 登记关联；关联不改变验收结论，场景执行会话不写测试。
