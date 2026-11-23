---
title: "经看板操作浏览器，产品备注取代项目验证技能，结果带缺口与保留意见"
scope: ["self:src/sop_board/operate.py", "self:src/sop_board/notes.py", "self:src/sop_board/changes.py", "self:src/sop_board/runs.py", "self:src/sop_board/status.py", "self:src/sop_board/store.py", "self:src/sop_board/config.py", "self:src/sop_board/entries.py", "self:src/sop_board/prompts/run.md"]
source: ["self:docs/design.md", "self:docs/reference.md", "用户决定（2026-11-23，已定决策 31 第二到第五段）"]
status: "accepted"
tags: ["决策 31", "browser do", "操作记录", "产品备注", "项目验证技能", "临时改动", "前置缺口", "带保留的通过", "格式 10"]
related: ["decisions/2026-11-23-decision-31-stage-one.md", "decisions/2026-11-12-shared-acceptance-records.md"]
---
# 经看板操作浏览器，产品备注取代项目验证技能，结果带缺口与保留意见

Flow Board 是独立的产品，使用方只是它的用户（用户 2026-11-23）。契约按 Flow Board 自己的概念定，每一条须对演示商城同样成立；某个使用方的数据只用来判断问题大小与先后。

- **操作经看板**：`browser do` 在运行持有的身份浏览器上执行 playwright-cli 步骤。守护进程为每个身份持有驱动会话，一次调用可以带多步，回复里直接带最后的页面结构（可按区域、层数裁剪）。每步记进运行目录 `steps.jsonl`，页面结构与截图存在 `steps/`，结果行可以直接把它们写作证据。
- **产品备注取代项目验证技能**：技能只在一台机器的数据目录里，同一产品的其他成员没有它。团队写给执行者的事改记为按产品共享的自由文本备注（挂在产品、入口或场景文件上），准备运行时写进 brief；执行会话可以提出备注（标为未经人确认）或报告不符。`skill` 与入口的 `driver` 字段取消。
- **起点与临时改动**：一组场景先只读核对起点，准备不了当场记受阻并写 `gap`，看板按 `gap` 原文归并。为准备起点改测试对象时用 `change add|restore` 登记与销账，提交时没恢复的登记待人处理。哪些对象可以改由团队写在备注里，看板不理解对象。
- **带保留的通过**：通过行可写 `reservation`。状态与通过计数不变，另给徽标与计数，人工复核队列把它们排前，人工确认率分带保留与不带保留两项。
- 业务记录格式升到 10：`notes` 表，`results.reservation`、`results.gap`。

## 备选
- 把产品事实搬进使用方仓库的验证手册：Flow Board 仍要靠每个产品自备一份特定文档，不采用。
- 按使用方的业务建模“测试世界”（组织、成员、团队）：只对一个使用方有意义，演示商城用不上，不采用。
- 带保留的通过单列为一种状态：会改变通过计数与既有筛选，先用徽标与单独计数。
- 每一步都自动截图：一天几千次调用会产生大量图片，改为执行会话在要核对的页面上按需 `--shot`，页面结构文本每次都留。

## 后果
还带着 `skill` 的 config.yaml 不合格，要先删掉这一行、把内容迁入备注。运行阶段不再生成 spec、录屏与 trace；`web_evidence` 的开关只对沉淀出的浏览器层守护有意义。prepare 仍会写 spec 运行用的材料（scope.json、PW_ 变量），随包 Web 技能也还是 spec 流程的写法，尚未清理。产品级定义（入口说明等）随产品共享这一项还没做，入口说明仍在各机器的 config.yaml。这几段只在合成数据与临时浏览器上检查过，没有在真实看板上运行。

### 放弃过的做法
- 每个项目在数据目录放一份项目验证技能，执行会话每轮先读它；入口可另指定辅助技能（driver）
  放弃原因：只在一台机器上，多人协作时别的成员没有；内容不受限，会积成几十 KB 的沉积
  重新考虑的条件：无
  来源：docs/design.md 已定决策 31 第五段；v10 的 config.yaml `skill` 字段
  关键词：skill、项目验证技能、driver、辅助技能、SKILL.md、skills 目录
- 运行阶段先写完整 spec、整条运行、再把 Playwright 报告转换成结果
  放弃原因：每条场景要多次往返才能写对 spec，定位猜错就整条重跑；已定决策 30 的数据里脚本失败占 12%～30%
  重新考虑的条件：无；spec 只在沉淀浏览器层守护时写
  来源：docs/design.md 已定决策 30 的 C 与已定决策 31 第二段
  关键词：spec、playwright convert、PW_CDP_ENDPOINT、global-setup、report.json
