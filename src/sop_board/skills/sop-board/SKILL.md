---
name: sop-board
description: 在已登记的工作区处理业务 SOP：登记已有 SOP 原文、根据 PRD 或需求写业务流程、生成验收场景、合并测试用例、执行 SOP 验收、复验或修复不通过的场景。Use for business SOP authoring, acceptance scenario generation, test-case merging, SOP verification, acceptance execution, and failed-scenario retesting or repair，包括写 SOP、验证 SOP、SOP 验收。
---

# Flow Board 会话入口

1. 先在产品仓库运行 `sop-board status --json`。工作区未登记时停止，按 `sop-board guide` 引导使用者运行 `sop-board init`。已登记但数据目录权限不足时，按 [sessions.md](references/sessions.md) 授予权限，不另建数据目录。有待处理事项时，先报告种类、原因、链接与可用的续接方式，询问是否先处理；本任务已有明确的处理顺序时沿用，不重复询问。
2. 按使用者要完成的阶段读取一份参考，再执行其中的步骤：

| 使用者要做的事 | 读取材料 |
| --- | --- |
| 根据 PRD、需求、评审记录或技术方案写业务 SOP | [编写业务 SOP](references/author-sop.md) |
| 登记已有业务 SOP 原文，或生成、修订、确认草稿 | [草稿](references/draft.md) |
| 上传并合并用例、回答合并缺口、本批不纳入或重新纳入 | [合并](references/merge.md) |
| 执行或重新验证已存在的场景 | [执行](references/execute.md) |
| 分析并修复不通过的场景、登记修复后的独立复验 | [修复与独立复验](references/fix.md) |

3. 需要认领或交接时读取[会话身份与独立执行](references/sessions.md)。操作要求以 `prepare` 输出的提示词为准；按它读取项目验证技能和产品手册，不在会话侧重写校验规则。
