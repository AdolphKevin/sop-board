# 经工作区适配环境执行 SOP 场景

任务目录：$task_directory

先读本轮速查卡 brief.md，按其中的阅读顺序取材料；entries.json、snapshot.json、regression-links.json 在下文用到时读取。速查卡写明本轮的地图用法、前置数据规则、时间上限与停止条件，与本提示词同为操作契约。

场景 expected 中标注“[用例 编号]”的细节预期也必须核对。cases.json 提供这些来源用例的前置条件、步骤和原始预期，在所属流程的本次执行中验证，不另建场景，也不遗漏细节。

$environment_preparation

先读取并遵循项目验证技能：$skill_path

入口在 entries.json 中有 driver_path 时（声明了 driver，或 Web 入口缺省使用随包的 playwright-acceptance），再读取并使用该辅助技能的正文；其 references/ 只在遇到对应情况时阅读。没有 driver_path 的入口只按项目验证技能操作。

$repository_instruction

## 凭据

- 入口 credential_env 只列变量名，取值已由${environment_origin}的环境传入本会话。按项目手册与本任务授权记录使用。
- 凭据只在进程环境和获授权的请求中使用：关闭命令跟踪；不打印取值或含凭据的请求；不写入脚本、截图、结果、日志或报告；证据先脱敏。地址变量同时声明为凭据时，也从环境读取。
- 缺变量的场景由看板写为受阻，并排除在本次 pending 之外，不能借用其他账号或凭据。
- 操作前只按名称核对执行命令能取得声明的变量；客户端另有过滤而未取得时，相关场景写 blocked，finding 只列缺少的变量名与原因，不尝试绕过客户端规则。

## 限制

${environment_restriction}不修改任何仓库中的产品文件，不写单元、接口或集成测试，不提交代码，不更改场景预期。根据真实入口的观察结果判定，不能只看源码或交互地图就判为通过。

## pending.json 的处理

**source-changed**：先依据以下材料判定是否受影响：

- changes.json 的完整提交范围、实际 git diff、场景内容与依赖关系；
- changes.json 中各仓库的 previous_uncommitted：上次结果执行时有未提交改动、现在内容已不同的文件，这些变化不在提交差异里，要一并判定；
- regression-links.json 中未过期关联测试的通过结果：须核对原始运行证据及其产品版本后，才可作为补充依据写进沿用理由，不能据此认定不受影响，也不能把历史通过写成当前运行；
- previous-results.json 中带 product_changed 的通过记录：所列路径（以 / 结尾表示整个仓库）在执行时可能处于中间状态，即使后来被撤销。场景依赖所列任一路径时，按无法证明不受影响处理。

判定的结果：

- 受影响，或无法证明不受影响：经真实入口重新执行。只有测试改动的，也要说明完整改动范围与依赖关系。
- 同一内容已有通过结果且判定不受影响：必须沿用，不再实际执行。status 仍为 pass，note 必须为“沿用：<具体改动范围、依赖关系和不受影响的理由>”，不得仅写“无影响”；对 product_changed 所列路径的判断也要写进理由。沿用的 evidence 可以为空数组，看板保留原通过记录的证据。
- 在任务目录写 impact.json：JSON 数组，恰好覆盖全部 source-changed 场景，每项为 scenario、affected（true 或 false）、reason。affected 为 false 的必须对应沿用结果，为 true 的必须对应实际执行结果。没有 source-changed 场景时写空数组。

**rerun**：必须实际执行并保存新证据，不得沿用。其他原因的场景也不得沿用。没有理由的沿用、遗漏场景或写出范围外的场景，都不合格。

## 结果

在任务目录写 results.jsonl，每条所选场景恰好一行：

- 字段：scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区的 ISO 8601）、executor、evidence（相对任务目录的路径数组），可选 finding、note、session，以及已登记问题的编号 issue（`sop-board issue list` 查看）。
- blocked、skip 必须有 finding。实际执行的 pass、fail 至少有一份真实证据，写入 evidence/。
- 证据面向内网的测试同事：路径写成相对仓库或任务目录的路径；凭据只写“已设置”之类的状态，不写本机绝对路径和凭据变量名。
- 不填 fingerprint；executor 与 session 由看板绑定到本次会话与任务。

## Web 场景

入口 driver 缺省或为 playwright-acceptance 的 Web 场景：

- 按内置技能 $playwright_skill 执行。PW_SCOPE、PW_SCENARIO_FILE 与 PW_ATLAS（有地图时）分别设为任务目录的 scope.json、scenarios.yaml、atlas.json。
- 在任务目录配置 workers: 1、retries: 0，同一 worker 复用 CDP。
- 按场景 verify 入口的 evidence 声明逐条留证；录屏与 trace 开关已按场景固定在 scope.json（显式 false 时不要求该附件），不自行覆盖。

转换报告用 sop-board playwright convert：--out 为任务目录，--scope 用 Web 快照，--scenario-file 用输入场景，--pending 用任务目录的 web-pending.json。

- 执行中途退出时，用 report.partial.json 加 --allow-partial 转换；未执行的场景成为 not-executed 标记行，登记时留在待执行范围。
- 按 source-changed 规则沿用部分场景时，Web 报告只转换实际执行的场景：以实际执行的编号清单作为 --pending，显式加 --allow-partial 后检查 coverage.json，再合并沿用行与实际执行行，最终覆盖总 pending。
- 混合入口的任务，把其他驱动的结果与 Web 结果合并回同一个 results.jsonl；不要用包含非 Web 场景的总清单要求 Playwright 执行。

完整结果交回看板登记，不要调用手工 submit，也不要使用 tw 命令。
