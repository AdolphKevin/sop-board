---
title: "登记由本机操作者自动署名，会话不询问确认人"
scope: ["self:src/sop_board/config.py", "self:src/sop_board/cli.py", "self:src/sop_board/authoring.py", "self:src/sop_board/skills/sop-board/**", "self:src/sop_board/prompts/**"]
source: ["self:docs/design.md", "self:docs/reference.md"]
verified: {"self": {"commit": "1bb388f063d4b7529bd5c7c6cf668608aea9ac86", "date": "2026-11-18"}}
guards: ["self:docs/sop/setup.yaml#SET-02-P01", "self:docs/sop/setup.yaml#SET-02-P02", "self:docs/sop/authoring.yaml#AUT-02-P04", "self:docs/sop/authoring.yaml#AUT-02-N02"]
tags: ["确认人", "署名", "操作者", "operator", "--as", "draft confirm"]
related: ["rules/draft-review-approval.md"]
---
# 登记由本机操作者自动署名，会话不询问确认人

CLI 的所有登记（上传、确认、标注、提问、配方、待人处理等）都以本机操作者署名：`~/.sop-board/operator` 中设置的姓名，缺省取 `git config user.name`。只有使用者明确说明是别人确认、上传或复测时，才用 `--as <姓名>`，且只影响这一次登记。不存在 --by、--author、--confirmed-by、--uploaded-by 参数。

草稿确认从已登记的同意中取署名。`draft approve` 由本机操作者本人同意，`draft confirm` 写入实际同意人（见 rules/draft-review-approval.md）。两条命令只接受记录本身的署名。

随包技能与提示词让会话只问业务内容（口径、预期、范围），不询问确认人、上传人或作者是谁，也不为等姓名结束一轮。内网表单的姓名由测试同学在顶栏填一次，存在浏览器 localStorage，各页面与场景共用；复测标注不填姓名也能提交，记为未署名。

## 依据
用户在 cw2 使用中反复被会话追问“确认人是谁”（v9 规则：姓名只在一次请求内有效，默认确认人须另行同意才设置）。v10 设计“署名”一节改为按机器设定一次、自动署名。用户决定（2026-11-12，已定决策 20）：cw2 有 3 份草稿共 32 条场景、批注为 0 就被确认，确认人记成运行命令的操作者，于是草稿确认改为以同意人为确认人。
