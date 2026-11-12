---
title: "格式 6 的数据库只新增表、打开时补建，不升格式号"
scope: ["self:src/sop_board/store.py"]
source: ["协调会话转达的实现约束（2026-11-12，决策 19、20 的实现任务）", "self:docs/design.md"]
status: "rejected"
related: ["decisions/2026-11-12-shared-acceptance-records.md"]
tags: ["数据库", "格式", "表结构", "user_version", "补建", "迁移", "draft_approvals", "service_events", "project-format"]
---
# 格式 6 的数据库只新增表、打开时补建，不升格式号

> 已被取代（2026-11-12，已定决策 21 的实现）：本机库升到格式 7，`ADDED_TABLES` 补建机制删除，`draft_approvals` 与 `service_events` 并回正式表结构；格式 6 的数据目录按 `project-format` 拒读。现行做法见 decisions/2026-11-12-shared-acceptance-records.md。

背景：已定决策 20 的草稿同意需要新存储。数据库格式一变，旧数据目录就以 `project-format` 拒读，而使用者本机有一个正在使用的真实项目数据目录（cw2），迁移脚本要到已定决策 21（格式 7、共享库）才有。

决定（2026-11-12 实现决策 20 时）：格式号保持 6。`store.ADDED_TABLES` 登记格式 6 之后新增的表；新库随 SCHEMA 一起建立，已有的格式 6 库在 `Store` 打开时检查并补建缺失的表（同一把跨进程锁内 `CREATE TABLE IF NOT EXISTS`），不改已有表的结构与数据。第一张这样的表是 `draft_approvals`（决策 20），第二张是 `service_events`（决策 22：运行中服务的停启与自动恢复）。

## 备选
- 升到格式 7 并写迁移：要先有迁移命令，且会让正在使用的数据目录在升级前被拒读；按设计，迁移留到决策 21 一次完成。
- 把同意存进已有的 `annotations` 表（加一种状态）：要改表的 CHECK 约束，等于改已有表的结构。

## 后果
- 只能“加表”。要改已有表的列或约束时，这条路走不通，须升格式号并提供迁移（已定决策 21 的格式 7 会一并处理，届时把 `ADDED_TABLES` 并回正式表结构）。
- 新版本打开旧的格式 6 库后，旧版本照样能读（不认识的表不影响它）；但旧版本看不到同意记录，草稿确认规则也不同。
