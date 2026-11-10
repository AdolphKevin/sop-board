---
title: "Flow Board 项目总览"
source: ["self:README.md", "self:docs/design.md", "self:docs/standard.md"]
---
# Flow Board 项目总览

Flow Board（命令 `sop-board`）是运行在开发者本机的 SOP 场景验收工具。场景 yaml 放在产品仓库，是唯一真相；AI 会话经 CLI 走真实入口执行，结果按场景指纹与产品快照登记；测试同学在看板上按场景复测和标注；修复后由独立会话复验；通过的场景沉淀成低成本测试，产品变化后先跑这些守护测试。

现在正按 [v10 设计](../docs/design.md) 整体重写（分支 v10-rewrite，旧实现见 tag v9-final），不保留双轨与兼容代码。设计的取舍见 decisions/2026-11-10-v10-rewrite.md。

从哪里读起：验收标准 docs/standard.md，场景写法 docs/sop-guide.md，本仓验收方法 docs/verification.md，代码在 src/sop_board/，随包技能在 src/sop_board/skills/。
