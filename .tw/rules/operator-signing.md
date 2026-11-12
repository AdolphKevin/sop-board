---
title: "登记由本机操作者自动署名，会话不询问确认人"
scope: ["self:src/sop_board/config.py", "self:src/sop_board/cli.py", "self:src/sop_board/authoring.py", "self:src/sop_board/skills/sop-board/**", "self:src/sop_board/prompts/**"]
source: ["self:docs/design.md"]
verified: {"self": {"commit": "5c06732e67a0171fdc1b689824aed6e4430f2425", "date": "2026-11-12"}}
guards: ["self:docs/sop/setup.yaml#SET-02-P01", "self:docs/sop/setup.yaml#SET-02-P02", "self:docs/sop/authoring.yaml#AUT-02-N02"]
tags: ["确认人", "署名", "操作者", "operator", "--as", "draft confirm"]
related: ["rules/draft-review-approval.md"]
---
# 登记由本机操作者自动署名，会话不询问确认人

CLI 的所有登记（上传、确认、标注、提问、配方、待人处理等）都以本机操作者署名：`~/.sop-board/operator` 中设置的姓名，缺省取 `git config user.name`。只有使用者明确说明是别人确认、上传或复测时，才用 `--as <姓名>`，且只影响这一次登记。不存在 --by、--author、--confirmed-by、--uploaded-by 参数。

例外是草稿确认：`draft confirm` 的确认人是在看板上同意该草稿各条场景的人（见 rules/draft-review-approval.md），命令没有 `--as`；使用者说“某人确认了草稿”时，会话也直接运行 `draft confirm`，不代填姓名。

随包技能与提示词让会话只问业务内容（口径、预期、范围），不询问确认人、上传人或作者是谁，也不为等姓名结束一轮。内网表单的姓名由测试同学在顶栏填一次，存在浏览器 localStorage，各页面与场景共用；复测标注不填姓名也能提交，记为未署名。

## 依据
用户在 cw2 使用中反复被会话追问“确认人是谁”（v9 规则：姓名只在一次请求内有效，默认确认人须另行同意才设置）。v10 设计“署名”一节改为按机器设定一次、自动署名。用户决定（2026-11-12，已定决策 20）：cw2 有 3 份草稿共 32 条场景、批注为 0 就被确认，确认人记成运行命令的操作者，于是草稿确认改为以同意人为确认人。
