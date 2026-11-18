---
title: "草稿每条场景都在当前内容上被同意、批注都处理后才能确认，确认人是同意人"
scope: ["self:src/sop_board/authoring.py", "self:src/sop_board/status.py", "self:src/sop_board/web.py", "self:src/sop_board/cli.py", "self:src/sop_board/assets/board.js", "self:src/sop_board/skills/sop-board/references/draft.md"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "1bb388f063d4b7529bd5c7c6cf668608aea9ac86", "date": "2026-11-18"}}
guards: ["self:docs/sop/authoring.yaml#AUT-02-P01", "self:docs/sop/authoring.yaml#AUT-02-P02", "self:docs/sop/authoring.yaml#AUT-02-P04", "self:docs/sop/authoring.yaml#AUT-02-N02", "self:docs/sop/authoring.yaml#AUT-02-N03", "self:docs/sop/authoring.yaml#AUT-02-N04", "self:docs/sop/authoring.yaml#AUT-02-N05"]
tags: ["草稿", "审阅", "同意", "批注", "draft approve", "draft confirm", "确认人", "待审", "有批注", "已同意", "节点面板", "文件面板"]
related: ["rules/operator-signing.md"]
---
# 草稿每条场景都在当前内容上被同意、批注都处理后才能确认，确认人是同意人

草稿场景的审阅状态只有三种，由记录现算：**有批注**（该场景有未处理的批注）优先；否则当前指纹上有未撤销的同意为**已同意**；其余为**待审**。同意按“文件 + 场景 + 指纹”记在看板数据库（`draft_approvals`），不写场景文件、不区分检出：任何检出中内容相同的场景都算已同意；场景四要素一改，原有同意随指纹失效。批注不采纳、场景未改时，原有同意仍有效；同意后又有人批注，以批注为准，同意记录仍留在审阅记录里。文件级批注不改变单条场景的状态，但阻止确认。

- 有未处理批注的场景不能同意（`draft-annotations-open`）；请求带的指纹与当前内容不符时拒绝（`draft-changed`），按节点同意时跳过并写明原因。
- 内网同意与撤销须署名，本机不填时以操作者署名；只能撤销自己的同意（`approval-not-own`），撤销只记时间、不删记录。
- 网页提供单条和按节点同意。使用者明确安排本人审阅时，`draft approve <文件> [场景编号…]` 以操作者署名，写入同一份审阅记录。省略编号时只处理待审场景，列出有批注的场景。点名时遇到不存在的编号或待处理场景批注，整批无写入。依据为用户决定（2026-11-18，已定决策 30 第一段）。
- `draft confirm <文件>` 要求每条场景已同意且没有未处理的批注（含文件级），否则业务失败（`draft-not-approved` 或 `draft-annotations-open`）并列出未同意的场景与未处理的批注。确认人写同意人：当前指纹上未撤销的同意，去重、按首次同意的先后、用“、”连接；没有 `--as`。
- 草稿的网页审阅入口是工作台“草稿”筛选。

## 依据
用户决定（2026-11-12，docs/design.md 已定决策 20）：原来 `draft confirm` 只要求没有未处理的批注，没人看过也能确认，确认人记为运行命令的操作者（cw2 当日有 3 份草稿共 32 条场景，批注为 0）；独立的草稿批注页不显示四要素，工作台批注卡只有一个文本框，测试同学不知道该批注什么，也没有“同意”。
