---
title: "待人工复核与人工确认率只认同一产品快照上的 Agent 独立真实入口通过"
scope: ["self:src/sop_board/status.py", "self:src/sop_board/reports.py", "self:src/sop_board/web.py", "self:src/sop_board/assets/board.js"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "d9c6dcb35eab074f7dff53ddd576536aabf0a08e", "date": "2026-11-10"}}
guards: ["self:docs/sop/workbench.yaml#WB-04-P01", "self:docs/sop/workbench.yaml#WB-04-N01", "self:docs/sop/workbench.yaml#WB-02-P03", "self:docs/sop/report.yaml#RPT-01-P02"]
tags: ["待人工复核", "人工复核", "复测标注", "人工确认率", "复核覆盖", "误判", "误判明细", "Agent 正确率", "mark"]
---
# 待人工复核与人工确认率只认同一产品快照上的 Agent 独立真实入口通过

“Agent 独立真实入口通过”指 kind=agent、independence 为 `independent`、不是保留通过（`reused_from` 为空）、执行期间产品无变化的通过。实现者自检、独立性未声明、保留通过和守护通过都不算。

**待人工复核**是徽标，不是状态，可与待沉淀并存。条件：场景状态为通过，且当前指纹最新一条结果是 Agent 独立真实入口通过。之后出现任一人工标注（通过、不通过、阻塞、需求变更），徽标就消失；产品变化使状态离开通过时，徽标也消失，因为人工复核要和 Agent 看同一产品快照。人工标注不通过时，场景直接显示为失败，Agent 结果保留在历史中。

**人工确认率**的样本是每一条 Agent 独立真实入口通过，不只取各场景最新一条。配对的人工结论：`of_result` 指向该样本、`product_digest` 与该样本相同的最近一条人工通过或不通过。分子是人工通过，分母是人工通过加不通过。只有阻塞或需求变更的样本记为未决，单列计数，不进分母。复核覆盖 = 有人工结论的样本数 / 全部样本数。人工结论为不通过的样本列入误判明细。执行者只在本机报告中显示，内网报告不显示。

不要用“各指纹最新人工标注”或“人工复测通过率”代替人工确认率。那两个口径不要求前面有 Agent 独立通过，不要求同一产品快照，还把阻塞和需求变更算进分母。

## 依据
使用者确认（2026-11-10）：v10 初版只给场景一个状态，丢掉了“Agent 通过、等待人工复核”的队列和误判统计。这与标准第 6 节“所需人工复核已完成”的交付条件不符，也算不出 Agent 独立验收经人工复核的正确率，因此补回。口径见 docs/design.md 已定决策 13 与“场景状态”，docs/reference.md 场景状态与“统计口径”表。误判要在每周复盘中逐条归因（Agent 判错、场景缺漏、断言缺漏、环境、人工标注有误），见 docs/standard.md 第 9 节。
