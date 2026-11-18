---
title: "按分支保存验收处置，业务库显式升级"
scope: ["self:src/sop_board/dispositions.py", "self:src/sop_board/store.py", "self:src/sop_board/status.py", "self:src/sop_board/scene_query.py", "self:src/sop_board/launcher.py", "self:src/sop_board/runs.py", "self:src/sop_board/reports.py"]
source: ["self:docs/design.md", "self:docs/reference.md", "用户决定（2026-11-18，已定决策 30 第一段）"]
status: "accepted"
tags: ["决策 30", "第一段", "处置", "由人验收", "搁置", "按场景查询", "执行代理", "保留意见", "数据库升级", "格式 8"]
related: ["decisions/2026-11-12-shared-acceptance-records.md", "rules/disposition-keeps-results.md", "rules/draft-review-approval.md"]
---
# 按分支保存验收处置，业务库显式升级

验收安排要能在看板上看到，并与真实验收结论同时存在。第一段同时补齐草稿本人同意、按场景查历史、用例编号、单次启动代理和登记说明。完整契约见 docs/reference.md。

- 业务记录格式 7 经显式事务升级到 8，新增处置表。普通业务读写校验版本，维护者统一升级共享库。本机状态独立使用 `user_version=8`，已有检出、修复基线和运行按原格式保留。
- 场景查询只从 Agent 结果关联的运行读取准备提交。人工标注和守护运行以自身的产品快照摘要标记版本。
- 单次启动的代理、模型、强度及附加参数留在启动目录。同次接续沿用首轮生效的设置，下一次启动再读项目配置。

## 备选

- 打开数据库时补建新表。拒绝原因是旧程序可能仍在读写旧业务格式；维护者需要先核对所有机器的看板与待提交运行。
- 用当前仓库提交补齐各类查询的取证版本。拒绝原因是当前提交无法证明历史记录在什么代码上取证。

## 后果

处置随业务记录跨机器共享，只在同名分支生效。没有共享库的项目用同一条升级命令原地迁移。本次升级目标由项目配置确定。
