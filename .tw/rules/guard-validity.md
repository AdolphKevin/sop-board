---
title: "守护通过只来自有效守护的实际运行，漏检使守护失效"
scope: ["self:src/sop_board/guards.py", "self:src/sop_board/status.py", "self:src/sop_board/skills/regression-distill/**"]
source: ["self:docs/design.md"]
verified: {"self": {"commit": "2a0b21464aa5ffc16d4bae12d60a73a458b87331", "date": "2026-11-10"}}
guards: ["self:docs/sop/guard.yaml#GRD-03-P01", "self:docs/sop/guard.yaml#GRD-03-N01", "self:docs/sop/guard.yaml#GRD-04-N01", "self:docs/sop/guard.yaml#GRD-02-N01"]
tags: ["守护", "沉淀", "retest", "regression.yaml", "漏检", "待沉淀"]
---
# 守护通过只来自有效守护的实际运行，漏检使守护失效

产品变化后，场景只有在以下条件全部满足时才记“守护通过”：守护有效；`retest`（守护进程执行，不启动 AI）实际运行了它引用的命令；每条命令退出码为 0；声明的测试名都在 junit 中且通过（跳过、缺失都不算）；运行前后产品快照不变。

守护有效指：场景指纹与登记时相同；测试文件是仓库内的普通文件且 sha256 一致；coverage 逐条、按顺序覆盖全部 expected 与 forbidden_effects，文字与当前一致；检出能力证据存在；acceptance 是同指纹、经真实入口的独立通过（实现者自检、未声明、保留通过都不算）；没有登记过漏检。只覆盖部分预期的守护不进入 regression.yaml。

守护通过之后，真实入口或人工复测发现失败时记一次漏检，该守护随即失效。只有同时取得漏检之后的新独立通过、并且测试摘要已变化，才能重新沉淀。

守护通过只用于产品变化后的复核；首次验收、场景内容变化与缺陷复验仍须真实入口执行。

## 依据
v10 设计“守护与沉淀”一节与使用者 2026-11-10 的决定：守护测试由 `sop-board retest` 直接运行；漏检后重新沉淀须有新的独立通过。
