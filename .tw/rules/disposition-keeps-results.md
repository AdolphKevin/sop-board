---
title: "处置控制自动验收范围，场景结论照常推导"
scope: ["self:src/sop_board/dispositions.py", "self:src/sop_board/status.py", "self:src/sop_board/runs.py", "self:src/sop_board/launcher.py", "self:src/sop_board/reports.py", "self:src/sop_board/web.py", "self:src/sop_board/assets/board.js"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "1bb388f063d4b7529bd5c7c6cf668608aea9ac86", "date": "2026-11-18"}}
guards: ["self:docs/sop/workbench.yaml#WB-09-P01", "self:docs/sop/workbench.yaml#WB-09-N01", "self:docs/sop/run.yaml#RUN-01-P04", "self:docs/sop/report.yaml#RPT-01-P03"]
tags: ["处置", "由人验收", "搁置", "执行范围", "pending", "all", "disposed"]
related: ["decisions/2026-11-18-decision-30-stage-one.md"]
---
# 处置控制自动验收范围，场景结论照常推导

按产品、文件、场景编号、分支登记由人验收或搁置。场景四要素变化后，处置沿用，结论按当前内容推导。人工标注和点名执行照常登记。命令行和本机看板可以设置或撤销，内网页面可以查看。

自动范围 `pending`、`all` 跳过本分支有处置的场景，点名时显示处置并继续执行。状态计数、普通队列和报告把本分支的处置场景单列。报告在处置章节保留原结论与证据。

## 依据

用户决定（2026-11-18，已定决策 30 第一段）。项目需要明确由谁验收、哪些场景本迭代暂缓，并保留场景当时的验收状态。
