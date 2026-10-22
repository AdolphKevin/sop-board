# 经工作区适配环境执行 SOP 场景

任务目录：$task_directory
读取 scenarios.yaml、cases.json、entries.json、pending.json、snapshot.json、changes.json、previous-results.json 与 regression-links.json。
场景 expected 中标注“[用例 编号]”的细节预期也必须核对；cases.json 提供这些来源用例的前置条件、步骤和原始预期，应在所属流程的本次执行中验证，不另建场景或遗漏细节。
看板已经用工作区配置启动了实际环境；按 entries.json 的 operate、observe 和 evidence 操作。
先读取并遵循项目验证技能：$skill_path
入口在 entries.json 中有 driver_path 时（声明了 driver，或 Web 入口缺省使用随包的 playwright-acceptance），再读取并使用该绝对路径的辅助技能；没有 driver_path 的入口只按项目验证技能操作。
地址从 entries.json 的 addresses 取得；各仓库目录由 SOP_REPO_<别名> 环境变量提供。
入口 credential_env 只列变量名，取值已由启动看板的环境传入本会话。按项目手册与本任务授权记录使用；凭据仅在进程环境和获授权请求中使用，关闭命令跟踪，不打印取值或含凭据的请求，不写入脚本、截图、结果、日志或报告，证据先脱敏。地址变量同时声明为凭据时也从环境读取。缺变量的场景由看板写为受阻并排除在本次 pending 之外，不能借用其他账号或凭据。
操作前只按名称核对执行命令能取得声明变量；若客户端另有过滤而未取得，相关场景写 blocked，finding 仅列缺少的变量名与原因，不尝试绕过客户端规则。

不启动或替换环境，不修改任何仓库中的产品文件，不写单元、接口或集成测试，不提交代码，不更改场景预期。
根据真实入口观察结果，不能只看源码就判为通过。

对于 pending.json 中 reason 为 source-changed 的场景，先依据 changes.json 的完整提交范围、实际 git diff、场景内容与依赖关系判定是否受影响。regression-links.json 中未过期关联测试的通过结果，须核对原始运行证据及其产品版本后，才可作为补充依据写进沿用理由，不能据此认定不受影响或把历史通过写成当前运行。受影响或无法证明不受影响时必须经真实入口重新执行；只有测试改动也须说明完整改动范围与依赖关系。
同一内容已有通过结果、且判定不受影响的场景必须沿用，不再实际执行：status 仍为 pass，note 必须为“沿用：<说明具体改动范围、依赖关系和不受影响的理由>”；不得仅写“无影响”。沿用的 evidence 可以为空数组，看板会保留原通过记录的证据。
在任务目录写 impact.json，内容为 JSON 数组，恰好覆盖全部 source-changed 场景，每项包含 scenario、affected（布尔值 true/false）、reason（具体判定理由）。
affected 为 false 必须对应沿用结果；affected 为 true（包括无法证明不受影响）必须对应实际执行结果，不得沿用。没有 source-changed 场景时写空数组。
其他 reason 不得沿用。没有理由的沿用、遗漏或范围外场景均不合格。
pending.json 中 reason 为 rerun 的场景必须实际执行并保存新证据，不得沿用。

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
