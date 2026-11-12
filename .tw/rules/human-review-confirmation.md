---
title: "待人工复核与人工确认率只认经真实入口的 Agent 独立通过，人工结论按 of_result 配对"
scope: ["self:src/sop_board/status.py", "self:src/sop_board/reports.py", "self:src/sop_board/web.py", "self:src/sop_board/assets/board.js"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "617395063356f4fb4c30868942012a5035b2d816", "date": "2026-11-12"}}
guards: ["self:docs/sop/workbench.yaml#WB-04-P01", "self:docs/sop/workbench.yaml#WB-04-N01", "self:docs/sop/workbench.yaml#WB-02-P03", "self:docs/sop/report.yaml#RPT-01-P02"]
tags: ["待人工复核", "人工复核", "复测标注", "人工确认率", "复核覆盖", "误判", "误判明细", "Agent 正确率", "mark"]
related: ["decisions/2026-11-11-business-driven-acceptance-scope.md", "decisions/2026-11-12-shared-acceptance-records.md"]
---
# 待人工复核与人工确认率只认经真实入口的 Agent 独立通过，人工结论按 of_result 配对

“经真实入口的 Agent 独立通过”指 kind=agent、independence 为 `independent`、`reused_from` 为空的通过。执行期间产品有变化的通过也算，变化的路径只作版本记录。实现者自检、独立性未声明、带 `reused_from` 的早先保留通过（未实际执行）都不算；守护运行不是结果，也不算。

**待人工复核**是徽标，不是状态，可与待沉淀并存。条件：场景状态为通过，且当前指纹最新一条结果是经真实入口的 Agent 独立通过。之后出现任一人工标注（通过、不通过、阻塞、需求变更），徽标就消失。代码改动不改变状态，所以不会让徽标消失；守护运行未通过使状态离开通过时，徽标随之消失。人工标注不通过时，场景直接显示为失败，Agent 结果保留在历史中。

**人工确认率**的样本是参与本看板推导的记录（各指纹上按本分支优先选出的结论，见 decisions/2026-11-12-shared-acceptance-records.md）中每一条经真实入口的 Agent 独立通过，不只取各场景最新一条；别的分支的通过在本分支有结论时不进样本。人工标注登记时 `of_result` 记当时该场景当前指纹最新一条 Agent 结果；样本的人工结论取 `of_result` 指向它的最近一条人工通过或不通过，不比较产品快照（快照只是版本记录）。分子是人工通过，分母是人工通过加不通过。只有阻塞或需求变更的样本记为未决，单列计数，不进分母。复核覆盖 = 有人工结论的样本数 / 全部样本数。人工结论为不通过的样本列入误判明细。执行者只在本机报告中显示，内网报告不显示。

不要用“各指纹最新人工标注”或“人工复测通过率”代替人工确认率。那两个口径不要求前面有 Agent 独立通过，还把阻塞和需求变更算进分母。

## 依据
用户确认（2026-11-10）：v10 初版丢掉了“Agent 通过、等待人工复核”的队列和误判统计，与标准第 6 节“所需人工复核已完成”的交付条件不符，因此补回。用户决定（2026-11-11）代码改动不改变场景状态，人工复核与确认率不再要求同一快照（decisions/2026-11-11-business-driven-acceptance-scope.md）。误判要在每周复盘中逐条归因（Agent 判错、场景缺漏、断言缺漏、环境、人工标注有误），见 docs/standard.md 第 9 节。
