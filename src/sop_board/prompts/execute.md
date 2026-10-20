# 经工作区适配环境执行 SOP 场景

任务目录：$task_directory
读取 scenarios.yaml、cases.json、entries.json、pending.json、snapshot.json、changes.json 与 previous-results.json。
场景 expected 中标注“[用例 编号]”的细节预期也必须核对；cases.json 提供这些来源用例的前置条件、步骤和原始预期，应在所属流程的本次执行中验证，不另建场景或遗漏细节。
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

入口 driver 缺省或为 playwright-acceptance 的 Web 场景先读取内置技能 $playwright_skill，设置 PW_SCOPE=$task_directory/scope.json、PW_SCENARIO_FILE=$task_directory/scenarios.yaml，
在任务目录配置 workers: 1、retries: 0；同一 worker 复用 CDP，每条用例独立保存截图、录屏和 trace。
需要补定位时只读探测，不修改业务预期或新建批次。使用 sop-board playwright convert 转换报告，
--out 必须为任务目录，--scope 使用 Web 快照、--scenario-file 使用输入场景，--pending 使用 任务目录的 web-pending.json。
混合入口任务将其他驱动的结果与 Web 结果合并回同一个 results.jsonl，最终覆盖总 pending.json；不要用包含非 Web 场景的总清单要求 Playwright 执行。
source-changed 的沿用仍遵守上述规则；实际执行的 Web 场景须有报告。不能因为部分场景受阻而漏行；blocked/skip 给出 finding。完整结果交回看板登记，
不要调用手工 submit，也不要使用 tw 命令。

若按 source-changed 规则沿用部分场景，Web 报告仅转换实际执行项：用实际执行编号清单作为 --pending，显式 --allow-partial 后检查 coverage.json；合并沿用行和实际执行行，最终覆盖总 pending，未实际执行且无合格沿用理由的项不得遗漏。
