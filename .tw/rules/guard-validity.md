---
title: "守护只在有效时运行，未通过使场景转待复测，最近一次守护运行通过后的失败记漏检"
scope: ["self:src/sop_board/guards.py", "self:src/sop_board/status.py", "self:src/sop_board/skills/regression-distill/**"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "60d1c86a0ad464e07ad86ad10898b99006e0b416", "date": "2026-11-12"}}
guards: ["self:docs/sop/guard.yaml#GRD-01-N02", "self:docs/sop/guard.yaml#GRD-05-P01", "self:docs/sop/guard.yaml#GRD-05-P02", "self:docs/sop/guard.yaml#GRD-05-N01", "self:docs/sop/guard.yaml#GRD-05-N02", "self:docs/sop/guard.yaml#GRD-03-P01", "self:docs/sop/guard.yaml#GRD-03-N01", "self:docs/sop/guard.yaml#GRD-03-P02", "self:docs/sop/guard.yaml#GRD-03-N02", "self:docs/sop/guard.yaml#GRD-04-N01", "self:docs/sop/guard.yaml#GRD-02-N01"]
tags: ["守护", "守护运行", "沉淀", "retest", "regression.yaml", "漏检", "待沉淀", "回归", "沉淀登记", "本机未同步", "本机无法运行"]
related: ["decisions/2026-11-11-business-driven-acceptance-scope.md", "decisions/2026-11-12-shared-acceptance-records.md"]
---
# 守护只在有效时运行，未通过使场景转待复测，最近一次守护运行通过后的失败记漏检

守护是回归网，用来发现代码改动带来的回归。`retest`（守护进程执行，不启动 AI）缺省取全部非草稿场景，只运行守护有效的；其余列在 `not_guarded` 并写明原因，不写守护运行，也不因此改变场景状态。

一次守护运行记为通过须同时满足：每条命令退出码为 0、没有超时；声明的测试名都在 junit 中且通过（跳过、缺失都不算）；运行前后产品快照不变。通过只留作证据，场景状态不变。未通过时写一条未通过的守护运行，场景转为待复测，原因以“守护测试未通过：”开头，待执行原因为 `guard-failed`；之后经真实入口通过，或守护重新运行通过，场景回到通过。判定比较业务记录库分配的统一序号（守护运行的序号晚于最新结果），不比较时间。守护未通过是非结论，只让产出它的机器和检出转待复测；别的机器在场景详情的“其他机器上的情况”里看到。

守护有效由每个看板按本检出的 regression.yaml 与测试文件现算，指：场景指纹与登记时相同；测试文件是仓库内的普通文件且 sha256 一致；coverage 逐条、按顺序覆盖全部 expected 与 forbidden_effects，文字与当前一致；检出能力证据在证据索引里有记录（文件可以在别的机器上）；acceptance 是同指纹、经真实入口的 Agent 独立通过（实现者自检、未声明、带 `reused_from` 的早先保留通过都不算）；没有登记过漏检。只覆盖部分预期的守护不进入 regression.yaml。

沉淀成功时把沉淀登记（场景、指纹、测试摘要、沉淀人、机器、时间）写进业务记录库；共享库里有当前指纹的登记而本检出没有这条守护或测试摘要不同时，仅在状态为通过时显示“已由 <沉淀人> 沉淀，本机未同步”，不显示待沉淀，`retest` 不运行它。守护引用的仓库在本机没有登记时列为“本机无法运行”（`retest` 的 `cannot_run`），不记未通过、不显示待沉淀。

漏检：同一指纹上本机本检出最近一次守护运行是通过，之后同一机器同一检出的真实入口或人工复测为失败、且其间本检出没有更晚的通过时，记一条漏检，该守护随即失效。别的机器上的失败不记本机守护的漏检；登记后的漏检按“文件 + 场景 + 指纹 + 测试摘要”各看板共用。最近一次守护运行未通过时，守护已经发现了问题，之后的失败不算漏检。重新沉淀须有漏检之后的新独立通过，且测试摘要已变化。

待沉淀只与通过并存：当前状态为通过、同指纹有经真实入口的 Agent 独立通过、且没有有效守护；本机未同步或无法运行时不进入待沉淀。徽标、筛选、计数、CLI 队列和报告统一使用这一条件。prepare 指名非通过场景及 submit 登记非通过场景均以 distill-not-passing 拒绝并写明当前状态。状态回到通过后重新计算显示；已有守护的有效性、漏检与 retest 规则保持原样（用户确认，2026-11-12，设计已定决策 25）。

守护运行不代替真实入口：首次验收、场景内容变化后的验收与缺陷复验仍须实际执行。

## 依据
v10 设计“守护与沉淀”一节；用户 2026-11-10 决定守护测试由 `retest` 直接运行、漏检后重新沉淀须有新的独立通过；用户 2026-11-11 决定守护是回归网、删除“守护通过”状态（decisions/2026-11-11-business-driven-acceptance-scope.md）。
