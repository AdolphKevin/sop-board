# 经工作区适配环境执行 SOP 场景

任务目录：$task_directory
读取 scenarios.yaml、cases.json、entries.json、pending.json、snapshot.json、changes.json 与 previous-results.json。
场景 expected 中标注“[用例 编号]”的细节预期也必须核对；cases.json 提供这些来源用例的前置条件、步骤和原始预期，应在所属流程的本次执行中验证，不另建场景或遗漏细节。
看板已经用工作区配置启动了实际环境；按 entries.json 的 operate、observe 和 evidence 操作。
先读取并遵循项目验证技能：$skill_path
入口声明了 driver 时，再读取并使用 entries.json 中该入口 driver_path 给出的辅助技能绝对路径；未声明时按项目验证技能操作。
地址从 entries.json 的 addresses 取得；各仓库目录由 SOP_REPO_<别名> 环境变量提供。

不启动或替换环境，不修改任何仓库中的产品文件，不写单元、接口或集成测试，不提交代码，不更改场景预期。
根据真实入口观察结果，不能只看源码就判为通过。

对于 pending.json 中 reason 为 source-changed 的场景，结合 changes.json 的提交范围、实际 git diff、场景内容与依赖关系判定是否受影响。受影响或无法证明不受影响时必须重新执行。
只有同一内容已有通过结果、且能明确说明改动为何不影响该场景时才可沿用：status 仍为 pass，note 必须为“沿用：<说明具体改动范围、依赖关系和不受影响的理由>”；不得仅写“无影响”。沿用的 evidence 可以为空数组，看板会保留原通过记录的证据。
其他 reason 不得沿用。没有理由的沿用、遗漏或范围外场景均不合格。
pending.json 中 reason 为 rerun 的场景必须实际执行并保存新证据，不得沿用。

在任务目录写 results.jsonl，每条所选场景恰好一行。
字段：scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区 ISO 8601）、executor、evidence（相对任务目录的路径数组），可选 finding、note、session。
blocked/skip 必须有 finding；实际执行的 pass/fail 至少一份真实证据，写入 evidence/ 目录。
不要填写 fingerprint；executor 与 session 最终由看板绑定到本次会话与任务。
