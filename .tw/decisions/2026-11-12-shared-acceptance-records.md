---
title: "业务记录可放在共享 PostgreSQL，按产品共享；结论按分支归属、本分支优先，非结论只影响产出它的检出"
scope: ["self:src/sop_board/store.py", "self:src/sop_board/status.py", "self:src/sop_board/runs.py", "self:src/sop_board/products.py", "self:src/sop_board/peers.py", "self:src/sop_board/works.py", "self:src/sop_board/guards.py", "self:src/sop_board/fixes.py", "self:src/sop_board/reports.py", "self:src/sop_board/web.py"]
source: ["self:docs/design.md", "用户 blur.yin 2026-11-12 已定决策 21", "协调会话转达的实现约束（2026-11-12，决策 21 的实现任务）"]
status: "accepted"
tags: ["共享库", "PostgreSQL", "产品", "加入", "机器", "machine.json", "格式 7", "统一序号", "本分支优先", "来源分支", "非结论", "其他机器上的情况", "run submit", "可重试", "证据", "对等读取", "implemented-by", "fix-not-local", "沉淀登记", "database-unreachable", "database-version", "product-unbound"]
related: ["decisions/2026-11-12-format-6-add-tables-only.md", "rules/guard-validity.md", "rules/human-review-confirmation.md"]
---
# 业务记录可放在共享 PostgreSQL，按产品共享；结论按分支归属、本分支优先，非结论只影响产出它的检出

背景：一个研发验证过的场景，另一个研发用 sop-board 启动同一个产品后看不到（全部状态都在运行看板那台机器的数据目录里）。

决定（已定决策 21 的实现，2026-11-12）：

- **两组表**：业务记录（产品、机器、运行记录的业务部分、结果与标注、证据索引、守护运行、漏检、沉淀登记、修复、收件箱、问答、批注、同意、待人处理）在业务记录库；本机状态（检出、`local_work`、`local_fixes`、启动、环境、浏览器、服务停启、身份计数、配方留痕）始终在本机 `board.sqlite3`。配了 `shared_database` 时业务记录库是 PostgreSQL，否则就是同一个 `board.sqlite3`（产品 ID `local`）。本机库升到格式 7，旧目录拒读，不迁移。
- **一套 SQL**：`store.DB` 统一 `?` 占位（PostgreSQL 时转 `%s`）、`RETURNING id`、`ON CONFLICT DO NOTHING`；只有自增主键的 DDL 写法分库。统一序号用 `counters` 表 `UPDATE … RETURNING`（行锁让序号顺序与提交顺序一致）。SQLite 写入靠本机文件锁；PostgreSQL 写入靠事务、唯一约束与条件更新（`… WHERE status='pending'`）。共享库连接放在小连接池里复用，新连接或闲置超过 5 秒的连接先核对表结构版本与产品（也起探活作用）。
- **跨库没有事务**：`run submit` 三步——证据文件进本机证据目录（幂等）、一个业务事务登记运行记录与结果（`work` 以 机器 + 本机库标识 + 本机运行编号 唯一，已登记就跳过）、本机状态与服务恢复。第二步失败时运行保持 prepared；第三步失败时重新提交只补第三步。本机库标识（`meta.instance`）防止重建的项目目录复用旧运行编号。
- **状态**：结论（Agent 通过与失败、人工标注、修复关闭）按记录的分支归属，本分支上有结论时只用本分支的，没有（或游离 HEAD）时用全部分支的并标来源分支；非结论（受阻、跳过、守护未通过）只取本机本检出的。先后只比较序号。历史与人工确认率样本都只用参与本看板推导的记录。
- **证据**：文件留在产出机器的数据根目录 `evidence/`。带 `--lan` 的看板把地址与对等令牌登记到 `machines`；别的看板在本机没有文件时凭令牌从 `/peer/evidence/<sha256>` 读取，核对 sha256 后转给页面，不保存。页面地址仍是本看板的 `/evidence/…`，内网页面不出现别的机器的地址。
- **产品与加入**：`init <项目>` 只列产品并以 `product-choice-required` 结束；绑定写在本机 `project.json`。

## 备选
- 浏览器直接从产出机器的看板加载证据：内网页面要暴露别的机器的地址，sha256 也没人核对；改为本看板中转。
- PostgreSQL 序列（`nextval`）做统一序号：并发提交时序号与提交顺序可能相反，且 SQLite 没有；改为计数行。
- 运行记录在 `run prepare` 时就写共享库：准备期间共享库断开就不能准备；设计只要求提交时登记。

## 后果
- 同一数据根目录（同一台机器）上绑定同一产品、检出名相同的两个项目被当成同一个检出（非结论与漏检会互相影响）。
- 守护的 `distill submit` 实际运行一次也是一条守护运行；在 A 上沉淀的守护，A 之后真实入口的失败按规则会记漏检（与 GRD-05-N02 的前置“ A 没有运行过 retest”不一致，已报告审核方）。
- 共享库的表结构有变化时须同时提供升级命令（这次没有）。
