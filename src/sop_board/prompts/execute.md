# 执行本轮 SOP 场景

任务目录：$task_directory
授权仓库：$repository
被测目录（快照提交的独立检出目录）：$candidate_directory
输入为 $scenario_file、$entries_file、$pending_file；快照为 $snapshot_file。
驱动技能目录：$skills_directory。

逐条按 verify 对应入口的 surface 选驱动：web 使用 playwright-acceptance；http 与 db
使用 verify-backend-sop-db；agent 使用 build-scenario-validation-loop；cli 使用临时 HOME；
其他 surface 按该入口 operate 说明操作。入口中的候选地址与目录以 entries.json 为准。
不修改授权仓库与 worktree 中的产品文件。不要修复产品、提交代码或修改场景预期。

在任务目录写 results.jsonl，每条所选场景恰好一行，不得含范围外场景。
使用 tw 结果契约字段：scenario、status（pass/fail/blocked/skip）、actual、executed_at
（带时区 ISO 8601）、executor、evidence（相对任务目录的路径字符串数组），可选 finding、note、session。
blocked 与 skip 必须有 finding；pass 与 fail 至少有一份证据。
证据文件必须实际写在任务目录 evidence/ 下。不要填写 fingerprint，内容指纹由看板快照确定。
executor 与 session 最终由看板记录为本次 Codex 会话编号与任务编号。
