你在隔离 worktree 的修复分支中修复产品。先读取以下绝对路径输入：
- 问题（批次、场景、实际结果、证据、缺陷单与此前 Agent 结果）：$problems_file
- 入口声明：$entries_file
- 固定场景与代码快照：$snapshot_file
- 用例并集：$cases_file
- 本次新增问题与上次验证证据：$feedback_file

对全部问题做一份根因分析，逐项判定 product、execution、environment、scenario、requirement 或 unfixable。
只修 product；其他问题写明理由与开发者方案，不为它们改代码。
从根因修复，直接替换错误逻辑，不加兼容层，不保留新旧两套实现，不做只让个别场景通过的局部修补，顾及其他场景与已有行为。
遵守仓库 AGENTS.md、CLAUDE.md 与已装技能；约定 TDD 时先写复现缺陷的失败测试。
docs/sop/ 下的文件与所用入口声明保持原样。改动留在 worktree，不提交，不推送。
最终输出严格符合 fix.schema.json：root_cause；problems 每项 batch、scenario、category、handling、reason；changed_files；tests 每项 command、result；plan（无方案用空字符串）。每个输入问题必须出现且只出现一次。

开发者说明：$developer_note
