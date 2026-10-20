# 经工作区适配环境执行 SOP 场景

任务目录：$task_directory
读取 scenarios.yaml、entries.json、pending.json、snapshot.json、changes.json 与 previous-results.json。
看板已经用工作区配置启动了实际环境；按 entries.json 的 operate、observe 和 evidence 操作。
声明了 driver 时使用该技能；否则 web 使用 playwright-acceptance，http/db 使用 verify-backend-sop-db，agent 使用 build-scenario-validation-loop，cli 经命令行执行。
地址从 entries.json 的 addresses 取得；各仓库目录由 SOP_REPO_<别名> 环境变量提供。

不启动或替换环境，不修改任何仓库中的产品文件，不写单元、接口或集成测试，不提交代码，不更改场景预期。
根据真实入口观察结果，不能只看源码就判为通过。

对于 pending.json 中 reason 为 source-changed 的场景，结合 changes.json 的提交范围、实际 git diff、场景内容与依赖关系判定是否受影响。受影响或无法证明不受影响时必须重新执行。
只有同一内容已有通过结果、且能明确说明改动为何不影响该场景时才可沿用：status 仍为 pass，note 必须为“沿用：<说明具体改动范围、依赖关系和不受影响的理由>”；不得仅写“无影响”。沿用的 evidence 可以为空数组，看板会保留原通过记录的证据。
其他 reason 不得沿用。没有理由的沿用、遗漏或范围外场景均不合格。

在任务目录写 results.jsonl，每条所选场景恰好一行。
字段：scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区 ISO 8601）、executor、evidence（相对任务目录的路径数组），可选 finding、note、session。
blocked/skip 必须有 finding；实际执行的 pass/fail 至少一份真实证据，写入 evidence/ 目录。
不要填写 fingerprint；executor 与 session 最终由看板绑定到本次会话与任务。
