---
design: "D-sop-board-7"
task: "SOP-BOARD-7"
revision: 3
assessment: {"complexity":"complex","risks":["persistence","public-contract","concurrency","authorization","cross-service"],"effort":"约三个工作日代理时长，分四段","drivers":["去掉令牌，开发者操作只在本机","工作区由多个仓库组成，项目差异写在代理生成的适配里","执行与复测使用适配启动的整套环境","修复跨仓库提交，全部可快进后一起合入"],"uncertainties":[],"required_skills":["Python","SQLite","子进程与进程组","Git worktree 与合并","HTML 与原生 JavaScript"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-7：v9 工作区、项目适配、跨仓修复与免令牌

> 本文件是 tw 任务 SOP-BOARD-7 的方案原文。行为依据是已批准的 [v9 方案](../proposals/2026-10-20-v9-workspace.md) D30–D36，本文只写实现时需要定下的接口；两处说法不同时以 v9 为准并修正本文。验收只以场景经真实入口执行为准，开发中不写单元、接口与集成测试。
>
> 状态：修订 2。修订 1 于 2026-10-20 经 AdolphKevin 批准；修订 2 按 v9 修订 2 改为只以场景验收：删除现有测试与回归检查运行器，开发中不写测试，细节用例并入流程场景；修订 3 按 v9 修订 3 改为由适配建立与删除 worktree。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
把看板从"一个项目一个仓库、两个令牌、内置三种候选方式"改为 v9：工作区由多个仓库组成；项目差异写在代理按随包指引生成的适配里；页面只填标记人；修复跨仓库提交、用修复代码复测、全部可快进后合入；合并按流程类与细节类划分用例。

非目标：页面上发起搭建；沉淀、守护与趋势页及回归检查运行器（删除）；Jira 回写（develop 上的代码保留，不纳入验收）；sop-verify 的改动（praxis-skills 另行处理）；旧数据目录的迁移（数据格式按新模型重建，旧目录不读取）。

- 需求级验收：A1、A2、A3、A4、A5

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | `sop-board`、`sop-board guide`、`sop-board check` | 安装、搭建、自检、启动 | 开发者与其代理 | 看板 | sop-board | A1 |
| self | /api/v1 与页面 | 上传、合并、执行、复核、修复、报告 | 开发者、测试同事 | 看板 | sop-board | A2、A3、A4、A5 |

## 访问与令牌
<!-- tw-section id=I1 type=interface depends=scope-global -->
按 v9 D31：删除 `SOP_BOARD_TOKEN`、`SOP_BOARD_ADMIN_TOKEN` 及其校验、页面身份栏中的令牌栏与浏览器存储项。开发者操作的接口（执行、重新合并、检查、修复的批准/重试/放弃、暂停与恢复、环境重启）只接受客户端地址为回环地址的请求，其余返回 403 `local-only`；非回环请求的页面不渲染这些按钮与环境日志。所有写入请求：带 `Origin` 时须与请求的 `Host` 同源，否则 403 `cross-origin`；JSON 接口要求 `Content-Type: application/json`，否则 415 `json-required`。标记人仍由页面填写并保存在浏览器。

## 工作区、配置与搭建指引
<!-- tw-section id=I2 type=interface depends=scope-global -->
按 v9 D30、D33。命令：`sop-board [工作区目录]`（缺省当前目录）、`--lan`、`--port`、`--data`；`sop-board guide` 输出随包的 `guide.md`；`sop-board check [工作区目录]` 运行自检。项目按 `uv tool install` 可安装。

数据目录缺省 `~/.sop-board/<工作区名>/`，工作区名取目录名；同名目录已登记为另一路径时追加路径摘要的前 8 位。配置为该目录下的 `config.yaml`：`repos`（别名 → 相对工作区的路径，别名为小写字母、数字与连字符）、`scenarios`（相对工作区、位于某个仓库内）、`entries`（场景入口格式，可加 `driver`）、`adapter`（`worktree`、`cleanup` 为可选命令数组，`env` 为必填命令数组，`ports` 为整数数组，`ready_timeout` 缺省 180）。命令中的相对路径相对数据目录解析。仓库身份由各仓库 origin 规范化得到；工作区以工作区名为键，替代原"项目"。

配置缺失或不合格时，看板照常启动，页面显示"尚未搭建"与指引中的做法，执行与修复停用；配置变化后需重新自检。README 增加"给代理"一节：安装命令与"先运行 `sop-board guide`"。`guide.md` 按 v9 D33 的条目写，保持通用。

## 适配环境与自检
<!-- tw-section id=I3 type=interface depends=scope-global,I2 -->
按 v9 D33 的契约。环境变量名 `SOP_REPO_<别名>` 中别名转大写、连字符转下划线；`SOP_BASE_<别名>` 同理。`adapter.worktree` 与 `adapter.cleanup` 的输入、输出与缺省行为按 v9 D33；看板核对 worktree 输出的每个路径：是该路径所在工作树的根目录、与对应基线仓库共用同一个 Git 仓库、当前分支等于 `SOP_FIX_BRANCH`，任一不符时本次建立失败并写明原因。适配运行器在独立进程组运行 `adapter.env`，在时限内等待标准输出出现 `sop-board: ready`；停止时 SIGTERM，10 秒后 SIGKILL；标准输出与错误输出写入数据目录 `env/<序号>.log`。同一时间只有一套环境，记录本次使用的各仓库目录与提交号。

自检五步按 v9 D33，结果写入数据目录 `checks/<时间>.json`（每步状态、说明、日志路径），`sop-board check` 以退出码 0/1 表示全部通过与否；页面"环境"栏显示最近一次自检、环境状态、最近 50 行日志（仅本机页面）与"重启环境"。自检全部通过且配置未变化时，看板启动时用基线目录启动环境。

## 执行使用适配环境
<!-- tw-section id=I4 type=interface depends=scope-global,I3 -->
执行任务替换原 `checkout`、`start`、`in-place` 三种方式：批次涉及的仓库（`metadata.repos`，缺省全部）有未提交的产品改动时任务为"待提交"；开始前若这些仓库的提交号与环境启动时不同，先重启环境；执行会话使用运行中的环境，入口地址由 `entries` 给出。产品变化后的执行由 Codex 按改动判定受影响的场景并重跑，不受影响的场景写明理由记为沿用。快照、作废、结果核对与登记沿用现有规则，产品指纹覆盖批次涉及的全部仓库。

## 跨仓修复
<!-- tw-section id=I5 type=interface depends=scope-global,I3,I4 -->
按 v9 D34：用 `adapter.worktree`（未声明时由看板用 `git worktree add` 建于任务目录 `repos/<别名>`）为每个涉及仓库建 `sop-board/fix/<编号>` 分支的 worktree 并核对，把其 `env` 用于修复会话与环境；结束时按 `adapter.cleanup`（未声明时用 `git worktree remove`）删除；修复会话以任务目录为工作目录，只改产品代码、不写测试；有改动的仓库各提交一次；用 worktree 重启环境，由新会话复测问题场景及受影响的场景；全部可快进后逐仓 `git merge --ff-only`，再用基线目录重启环境；任一仓库不可快进时都不合入，转"待开发者合入"并逐仓列原因。修复会话的判定类别与转开发者沿用现有规则。

## 用例分层与报告
<!-- tw-section id=I6 type=interface depends=scope-global -->
按 v9 D35（修订 2）：合并输出模式为每条用例增加 `kind`（`flow`/`detail`）、`kind_reason`，`detail` 用例另有 `merged_into`（流程场景编号）；细节用例的预期并入该场景的 `expected`，不单独成场景；合并页按流程场景列出并入的细节用例与理由；开发者可把某条细节用例改为单独成场景（本机操作）并触发重新合并。报告的"用例覆盖"写明每条细节用例并入的场景。

## 验收 A1：搭建与访问
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1,I2,I3 -->
SOP-71、SOP-73 节点全部场景通过。

- 场景：docs/sop/sop-board-7.yaml#SOP-71、SOP-73
- 依据：位置 docs/proposals/2026-10-20-v9-workspace.md 的 D30、D31、D33；版本 v9（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A2：合并与执行
<!-- tw-section id=A2 type=acceptance depends=scope-global,I3,I4 -->
SOP-72 节点全部场景通过。

- 场景：docs/sop/sop-board-7.yaml#SOP-72
- 依据：位置 docs/proposals/2026-10-20-v9-workspace.md 的 D32、D33；版本 v9（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A3：跨仓修复
<!-- tw-section id=A3 type=acceptance depends=scope-global,I5 -->
SOP-74 节点全部场景通过。

- 场景：docs/sop/sop-board-7.yaml#SOP-74
- 依据：位置 docs/proposals/2026-10-20-v9-workspace.md 的 D34；版本 v9（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A4：用例分层与报告
<!-- tw-section id=A4 type=acceptance depends=scope-global,I6 -->
SOP-75 节点全部场景通过。

- 场景：docs/sop/sop-board-7.yaml#SOP-75
- 依据：位置 docs/proposals/2026-10-20-v9-workspace.md 的 D35；版本 v9（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A5：真实项目
<!-- tw-section id=A5 type=acceptance depends=scope-global,I5 -->
AdolphKevin 在真实多仓项目上按[验收计划](../acceptance/2026-10-20-real-project-main-flow.md)执行段 1、段 2 的场景；流程与缺陷的选择到时由他确认。

- 场景：docs/sop/sop-board-7.yaml#SOP-76
- 依据：位置 docs/acceptance/2026-10-20-real-project-main-flow.md；版本 草案（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 任务 T1：免令牌与删除测试
<!-- tw-section id=T1 type=task depends=scope-global,I1 -->
实现 I1；删除 `tests/` 下全部测试及 pytest 相关的配置与依赖，README 与 docs/verification.md 改为以场景验收的说明；`.tw/config.json` 的检查 `unit` 在 tw 支持空检查后去掉（tw 方案修订 2 的运行时改动，须在本任务验收前发布）。

- 验收：A1
- 范围：src、tests、README.md、pyproject.toml、uv.lock、docs/designs、docs/sop、docs/acceptance、docs/proposals、.tw/config.json
- 前置任务：无

## 任务 T2：工作区、适配与执行
<!-- tw-section id=T2 type=task depends=scope-global,I2,I3,I4 -->
实现 I2、I3、I4，删除 `candidates`、`--writable-repo` 与回归检查运行器（`checks`）；修复流程在 T3 改造前继续使用单仓库 worktree 并改用适配环境复测。

- 验收：A1、A2
- 范围：src、tests、README.md、pyproject.toml、uv.lock
- 前置任务：无

## 任务 T3：跨仓修复
<!-- tw-section id=T3 type=task depends=scope-global,I5 -->
实现 I5。

- 验收：A3、A5
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T4：用例分层与报告
<!-- tw-section id=T4 type=task depends=scope-global,I6 -->
实现 I6。

- 验收：A4
- 范围：src、tests、README.md
- 前置任务：无
