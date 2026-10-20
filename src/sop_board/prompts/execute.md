# 执行本轮 SOP 场景

任务目录：$task_directory
授权仓库：$repository
被测目录（快照提交的独立检出目录）：$candidate_directory
输入为 $scenario_file、$entries_file、$pending_file；快照为 $snapshot_file。
Web 内置技能：$playwright_skill。
其他入口的驱动技能目录：$skills_directory。
Web 执行范围快照：$scope_file；Web 待执行编号：$web_pending_file。

逐条按 verify 对应入口的 surface 选驱动：web 使用 playwright-acceptance；http 与 db
使用 verify-backend-sop-db；agent 使用 build-scenario-validation-loop；cli 使用临时 HOME；
其他 surface 按该入口 operate 说明操作。入口中的候选地址与目录以 entries.json 为准。
不修改授权仓库与 worktree 中的产品文件。不要修复产品、提交代码或修改场景预期。

在任务目录写 results.jsonl，每条所选场景恰好一行，不得含范围外场景。
使用 sop-board 结果契约字段：scenario、status（pass/fail/blocked/skip）、actual、executed_at
（带时区 ISO 8601）、executor、evidence（相对任务目录的路径字符串数组），可选 finding、note、session。
blocked 与 skip 必须有 finding；pass 与 fail 至少有一份证据。
证据文件必须实际写在任务目录 evidence/ 下。不要填写 fingerprint，内容指纹由看板快照确定。
executor 与 session 最终由看板记录为本次 Codex 会话编号与任务编号。

Web 任务先读取上面的内置技能，设置 PW_SCOPE=$scope_file、PW_SCENARIO_FILE=$scenario_file，
在任务目录配置 workers: 1、retries: 0；同一 worker 复用 CDP，每条用例独立保存截图、录屏和 trace。
需要补定位时只读探测，不修改业务预期或新建批次。使用 sop-board playwright convert 转换报告，
--out 必须为任务目录，--scope 使用 Web 快照、--scenario-file 使用输入场景，--pending 使用 Web 待执行编号文件。
混合入口任务将其他驱动的结果与 Web 结果合并回同一个 results.jsonl，最终覆盖总 pending.json；不要用包含非 Web 场景的总清单要求 Playwright 执行。
不能因为部分场景受阻而漏行；blocked/skip 给出 finding。完整结果交回看板登记，
不要调用手工 submit，也不要使用 tw 命令。
