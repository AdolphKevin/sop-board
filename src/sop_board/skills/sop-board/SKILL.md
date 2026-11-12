---
name: sop-board
description: 写 SOP、生成验收场景、合并用例、执行/复测 SOP 场景、安排分步实现的验收时机、修复失败场景、沉淀回归测试：在已登记 sop-board 项目的产品仓库或 worktree 中经 sop-board CLI 完成，也处理草稿批注与确认、向测试同学提问、交互地图、前置数据与待人处理。Use for business SOP authoring, acceptance scenario drafting, test-case merging, scenario execution and retest, planning when to accept multi-step work, failed-scenario repair, and regression distillation.
---

# sop-board 会话入口

Agent 工作只有一条路径：在产品仓库的检出目录运行 `sop-board` 命令。prepare 类命令输出 `prompt` 时，读这份 prompt.md 并严格按它执行，它是这项工作的操作契约；结果、确认与标注只经对应的 submit、confirm、mark 等命令登记。

## 每次都适用

1. **先看状态**：在当前目录运行 `sop-board status --json`。项目和检出（main 或 git worktree）按当前目录自动识别，直接沿用识别结果。识别不到项目时，按 `sop-board guide` 引导使用者运行 `sop-board init`。`attention` 中有待人处理事项时一并报告。返回 `product-unbound`，或使用者要把项目接到团队的共享库时，先按 [references/shared.md](references/shared.md) 接入；返回 `database-unreachable`、`database-version` 时照输出报告（连的哪台主机、两边的版本），等使用者处理。
2. **守护进程**：`env`、`browser`（install 除外）、`run prepare`、`run launch`、`retest`、`distill submit` 依赖看板守护进程。`daemon.running` 为 false 或命令返回 `daemon-not-running` 时，在后台启动 `daemon.command`（形如 `sop-board serve <项目>`，使用者已允许），再次 status 确认运行中后继续。
3. **署名自动**：所有登记以本机操作者署名（`sop-board operator show`；缺省取 `git config user.name`，`sop-board operator set <姓名>` 修改）。直接运行登记命令；只有使用者明确说是别人确认、上传或复测的，才加 `--as <他说的姓名>`。草稿确认例外：`draft confirm` 的确认人是在看板上同意场景的人，命令自动写入。会话只问业务内容（口径、预期、范围），身份交给命令：不询问确认人、上传人或作者是谁，不为等姓名结束一轮，不编造姓名。
4. **会话编号**：`run prepare`、`fix prepare`、`distill prepare` 自动取本会话编号（`run launch` 启动的会话取 `SOP_BOARD_SESSION`，Codex 取 `CODEX_THREAD_ID`，Claude Code 取 `CLAUDE_CODE_SESSION_ID`）；都取不到时命令会要求 `--session`。
5. **多步工作先定验收安排**：一项需求分几步实现，或几项需求接连实现时，在改动任何文件之前，按 [references/run.md](references/run.md)「分步实现时的验收时机」对每一步定下“做完就验”或“并到后面一起验”，把安排和依据的事实告诉使用者，使用者确认或调整后再动手。之后每一步的报告都标明它已独立验收、只做了实现者自检还是未验证。
6. **宣布可验收前**：准备宣布“可以验收”或交付前，运行 `sop-board status --json`，报告 `counts` 中待复测（retest）、失败（fail）、受阻（blocked）、未执行（not-run）的数量与 `queues` 中的场景，这些须经真实入口执行。代码改动不改变场景状态；有沉淀的守护时，先运行 `sop-board retest` 检查代码回归。分几步实现的，先前通过、但被后续步骤重做了实现的场景仍显示通过、不在 `queues` 中：守护有效且最近一次运行通过的仍然可信，其余按 run.md「统一验收：选范围」列入验收范围、重新执行。报告之后才宣布。

## 按请求读取一份参考

| 使用者要做的事 | 读取 |
| --- | --- |
| 根据 PRD、需求写业务 SOP，或登记已有 SOP 原文 | [references/author-sop.md](references/author-sop.md) |
| 从 SOP 生成验收场景草稿、处理草稿批注、查看审阅进度、确认草稿 | [references/draft.md](references/draft.md) |
| 上传、合并测试用例，向测试同学提问、读取答复 | [references/merge.md](references/merge.md) |
| 执行、验收或重跑场景，未执行与受阻的口径，依赖故障的场景（env stop/start），分步实现的验收安排与统一验收，登记人工复测结论，真人登录 | [references/run.md](references/run.md) |
| 运行守护测试检查回归（retest）、沉淀回归测试 | [references/guard.md](references/guard.md) |
| 修复失败场景、复验修复、关闭修复 | [references/fix.md](references/fix.md) |
| 梳理 Web 交互地图 | [references/atlas.md](references/atlas.md) |
| 前置数据、身份计数、待人处理、检出与环境（服务状态）、导出报告 | [references/support.md](references/support.md) |
| 接入共享库（新建或加入产品），验收别人机器上实现的功能，别的机器产出的修复（`fix-not-local`）、守护（“本机未同步”“本机无法运行”）与证据，来源分支与“其他机器上的情况” | [references/shared.md](references/shared.md) |
