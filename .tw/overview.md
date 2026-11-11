---
title: "Flow Board 项目总览"
source: ["self:README.md", "self:docs/design.md", "self:docs/standard.md"]
---
# Flow Board 项目总览

Flow Board（命令 `sop-board`）是运行在开发者本机的 SOP 场景验收工具。场景 yaml 放在产品仓库，是唯一真相；AI 会话经 CLI 走真实入口执行，结果按场景指纹登记并记下取证时的产品快照，哪些场景要验收由场景内容决定、代码改动不改变状态；测试同学在看板上按场景复测和标注；修复后由独立会话复验；通过的场景沉淀成低成本守护测试，由 retest 运行来发现代码回归。

设计见 [docs/design.md](../docs/design.md)（v10；旧实现见 tag v9-final），取舍见 decisions/2026-11-10-v10-rewrite.md。命令与文件契约见 docs/reference.md。

从哪里读起：验收标准 docs/standard.md，场景写法 docs/sop-guide.md，本仓验收方法 docs/verification.md。验收用的演示项目在 examples/demo-shop/（make_demo.py 一条命令生成 app、lib 两仓并登记）。代码在 src/sop_board/：cli.py 是全部命令，server.py 是守护进程，runs.py、guards.py、fixes.py、browsers.py 是主要流程；随包技能在 src/sop_board/skills/。
