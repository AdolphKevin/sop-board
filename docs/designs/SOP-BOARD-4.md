---
design: "D-sop-board-4"
task: "SOP-BOARD-4"
revision: 2
assessment: {"complexity":"complex","risks":["persistence","public-contract","concurrency","authorization"],"effort":"约两个半工作日代理时长","drivers":["看板在授权仓库的已提交代码上自动执行场景，每轮一个新的 Codex 会话","三种候选版本验证方式与快照核对决定一轮结果是否有效","浏览器独占与暂停让自动任务不干扰开发者手工使用","回归检查运行器在检出目录运行项目声明的检查并核对声明的用例"],"uncertainties":[],"required_skills":["Python","SQLite","子进程与并发","Git worktree","JUnit XML 与 go test -json","HTML 与原生 JavaScript"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-4：看板发起的执行与回归检查

> 本文件是 tw 任务 SOP-BOARD-4 的方案原文。总体设计见本仓 [docs/proposals/2026-10-19-standalone-sop-verification.md](../proposals/2026-10-19-standalone-sop-verification.md)（v8），本需求对应其第 8 节 SOP-BOARD-4 一行，以及 D2、D4、D9、D21、D22、D23 与 S2 中属于本需求的部分。验收场景见 [docs/sop/sop-board-4.yaml](../sop/sop-board-4.yaml)。
>
> 状态：修订 2。修订 1 于 2026-10-20 经 AdolphKevin 授权由 Claude 代批；修订 2 按测试代理 sop-board-4-tester 的 31 个待确认问题与 6 处出入，以总体方案 v8 为准补写，同日代批。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
在授权仓库（SOP-BOARD-3 的 `--writable-repo`）中，看板自己执行场景：合并结果写入后自动执行新增与内容变化的场景，开发者也可以发起执行全部待执行场景。执行只针对已提交的代码：工作区有未提交的产品改动时任务停在"待提交"。每轮执行固定一个快照，由一个新的 Codex 会话按入口选驱动执行，看板核对结果契约后登记为 Agent 轮次；快照在执行中变化则本轮作废。候选版本按入口的 `candidate` 取 `checkout`、`start` 或 `in-place` 验证。浏览器入口的任务独占测试专用浏览器，开发者可以暂停自动任务。由回归声明守护的场景不经浏览器执行，看板在同一快照上运行其检查并核对声明的用例。

非目标：修复闭环、复测与合入（SOP-BOARD-5）；沉淀任务、守护与漏检（SOP-BOARD-6）；未授权项目的执行（仍由 sop-verify 在开发者会话中完成）。

技术选择沿用 SOP-BOARD-1 至 3：标准库子进程与 `sqlite3`，Codex 经 SOP-BOARD-3 的运行器调用，Git 经子进程调用，环境白名单同 SOP-BOARD-3 I3。

- 需求级验收：A1、A2、A3、A4、A5

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | 授权仓库的入口声明 | 读取入口与检查声明 | 开发者 | 看板 | sop-board | A1 |
| self | /api/v1 开发者操作 | 发起执行、发起回归检查、暂停与恢复自动任务 | 开发者页面 | 看板任务队列 | sop-board | A2、A4、A5 |
| self | 执行任务与检查任务 | 建 worktree、启动候选服务、调用 Codex、运行检查、登记轮次 | 看板 | 批次统计与页面 | sop-board | A2、A3、A5 |
| self | 批次页面 | 显示执行状态、任务、检查结果与作废原因 | 看板 | 测试岗、开发岗 | sop-board | A4 |

## 入口与检查声明
<!-- tw-section id=I1 type=interface depends=scope-global -->
看板按 SOP-BOARD-3 I4 的顺序读取授权仓库的入口声明（有 `.tw/config.json` 时取其 `acceptance_entries` 与 `checks`，否则取 `docs/sop/entries.yaml` 的入口与顶层 `checks`）。入口字段同 tw 的 `acceptance_entries`（`surface`、`operate`、`observe`、`evidence`、`isolation`，可选 `address_env`），另有可选字段：

- `candidate`：`checkout`、`start` 或 `in-place`，缺省 `in-place`。
- `start`：字符串数组，`candidate` 为 `start` 时必填，在检出目录中运行以启动服务。
- `reload`：字符串数组，可选，供 SOP-BOARD-5 在原地替换后运行。

检查字段同 tw 的 `checks`：`argv`（字符串数组，`{output}` 替换为该次运行的输出目录）、`cwd`（相对检出目录，缺省 `.`）、`timeout`（秒，缺省 900）、`format`（`junit` 或 `go-json`）、`report`（`junit` 时必填，可含 `{output}`）、`required_env`（环境变量名数组，缺省空）；其余字段忽略。

声明不合格（字段类型不对、`candidate` 取值不对、`start` 缺失、检查缺 `argv`、格式不对或 `junit` 缺 `report`、场景的 `verify` 引用未声明的入口、`regression.check` 引用未声明的检查）时，批次页面显示"入口声明不合格"及问题；排队中的执行与检查任务不开始，显示同样的原因，声明恢复合格后自动开始；开发者此时发起执行或检查返回 409 `entries-invalid`；运行中的任务按 I2 的快照核对处理。`GET /api/v1/projects/<项目键>/entries`（不需要令牌）返回 `{ok, source, entries, checks, problems}`：`ok` 在没有问题时为 true；`source` 为 `tw-config` 或 `entries-yaml`；入口与检查都回填缺省值（`candidate` 为 `in-place`，`cwd` 为 `.`，`timeout` 为 900，`required_env` 为空数组）。项目不存在返回 404 `project-not-found`，未授权项目返回 409 `repo-not-writable`。

## 执行任务
<!-- tw-section id=I2 type=interface depends=scope-global,I1,D1 -->
**触发**。授权仓库中：SOP-BOARD-3 的合并任务把结果写入工作区并登记为新场景版本后，看板为该批次建执行任务，范围是这一版本中新增与内容变化的场景；批次没有用例（因而不会合并）时，经发现或推送登记的新场景版本同样触发，范围相同；开发者 `POST /api/v1/projects/<项目键>/batches/<批次>/executions`（开发者令牌）建执行任务，范围是总体方案 S2 的全部待执行场景：没有结果、内容变化、当前指纹最新结果为 `fail`、Agent 通过后产品已变化的场景（最新结果为 `blocked` 或 `skip` 的不在其中），返回 `{ok, task}`。范围中声明了 `regression.check` 的场景只运行检查（I5）；范围只剩这类场景时任务只运行检查，不启动执行会话。范围为空时不建任务（开发者发起时返回 409 `nothing-to-execute`）。同一批次已有排队中、待提交或等待浏览器的执行任务时，新的范围并入该任务，返回该任务；已运行中的不变，新的任务排在其后。未授权项目发起执行返回 409 `repo-not-writable`。

**已提交代码原则**。任务开始前，看板计算授权仓库工作区的产品指纹（SOP-BOARD-3 D2，与 SOP-VERIFY-1 D2 相同：已跟踪与未忽略的文件取工作区内容）与 HEAD 的产品指纹（同样的排除规则，取 HEAD 提交中的文件）。两者不同时（含未跟踪且未忽略的产品文件）任务为"待提交"，页面写明"工作区有未提交的产品改动"并列出前 10 个不同的路径；看板每 5 秒复核，相同后任务开始。只改了 `docs/sop/` 不算产品改动。待提交的任务不占 Codex 名额。

**快照**。任务开始时固定并写入任务目录 `snapshot.json`：`scenario_file_sha256`、`scenarios`（所选场景编号到内容指纹）、`entries_sha256`（入口与检查声明的规范化 JSON 的 sha256）、`cases_sha256`（用例并集内容摘要，没有用例时为空字符串）、`commit` 与 `product_digest`（工作区产品指纹）。执行结束时与登记前各核对一次：授权仓库工作区的产品指纹、HEAD 提交、批次当前场景版本与所选场景的内容指纹、入口与检查声明摘要、用例并集摘要有任何不同（含只有未提交改动），本轮作废：任务为"已作废"，写明哪一项变化，不登记结果。

**被测对象**（按每条场景所用入口的 `candidate`，见 I3）。

**执行会话**。每个执行任务一个新的 Codex 会话（SOP-BOARD-3 运行器的新会话命令），沙箱 `workspace-write`，可联网，工作目录为任务目录。任务目录中放入：`scenarios.yaml`（只含所选场景的场景文件，节点与 `metadata` 保留）、`entries.json`、`pending.json`（数组，每项 `{scenario, reason}`，原因取 `new`、`changed`、`failed`、`source-changed`）、`evidence/` 目录与提示词 `prompt.md`（随看板发行的模板 `src/sop_board/prompts/execute.md` 填入路径）。提示词要求：按入口的 `surface` 选驱动（`web` 用 playwright-acceptance，`http` 与 `db` 用 verify-backend-sop-db，`agent` 用 build-scenario-validation-loop，`cli` 用临时 `HOME`，其他取值按入口的 `operate` 说明操作），驱动技能取自环境变量 `SOP_BOARD_SKILLS_DIR`（缺省 `~/.agents/skills`）；不修改授权仓库与 worktree 中的产品文件；在任务目录写 `results.jsonl`（tw 结果契约字段，证据路径相对任务目录且在 `evidence/` 下）。会话结束后看板按结果契约与 SOP-BOARD-1 I2 的轮次规则核对：每条所选场景恰好一行、没有范围外的场景，证据文件存在且在 `evidence/` 下，`blocked` 与 `skip` 有 `finding`；不合格时任务为"失败"并在 `check.json` 列出问题，不登记。合格时上传证据并登记为 Agent 轮次：执行者为 Codex 会话编号，会话为任务编号，实现者为授权仓库 `git config user.name` 的值（没有时省略），产品取快照中的提交号与指纹，`dirty` 为 false；`in-place` 入口的结果附 `note`"运行环境按 HEAD 记录，未经核实"。

**状态**。执行任务的状态取：排队中、待提交、等待浏览器、运行中、已完成、失败、已作废、已中断、已放弃、Codex 不可用。开发者操作沿用 SOP-BOARD-3 I2 的重试与放弃：放弃另允许待提交与等待浏览器的任务；重试对失败、已作废、已中断与 Codex 不可用的任务以原范围重新排队，重新核对待提交并重新固定快照。任务接口另返回 `scope`（场景编号与原因）、`snapshot`、`void_reason` 与 `rounds`（登记的轮次号）。

## 候选版本
<!-- tw-section id=I3 type=interface depends=scope-global,I1,I2 -->
- `checkout` 与 `start`：看板在数据目录 `worktrees/<任务编号>/` 以 `git worktree add --detach` 检出快照提交（授权仓库当前分支的最新提交），执行会话在该目录中操作；任务结束（任何结局）后 `git worktree remove --force` 移除。看板不在授权仓库中建分支。
- `start`：看板以检出目录为工作目录、白名单环境运行入口的 `start` 命令，环境变量 `SOP_PORT` 为看板分配的空闲端口，入口的 `address_env` 指定的变量为 `http://127.0.0.1:<端口>`，同一变量也写入执行会话的环境；120 秒内该地址的 HTTP 请求得到任何响应视为就绪。命令在就绪前退出（如端口被占）或 120 秒内未就绪时，按一次未通过处理（总体方案 D23）：本轮所选的使用该入口的场景都登记为 `fail`，`actual` 写"候选版本启动失败：<命令退出码与输出末行，或就绪超时>"，执行者为"候选启动 <入口名>"，启动日志 `start.log` 作为证据，与同一任务中其他入口的执行会话结果登记在同一轮；所选场景全部使用该入口时不启动执行会话。任务结束与看板退出时终止该命令的进程组。
- `in-place`：执行会话测开发者正在运行的环境；入口的 `address_env` 指定的变量从看板自身的环境传给执行会话（白名单之外只放行这些变量名）；看板环境中没有该变量时，使用该入口的场景登记为 `blocked`，`finding` 写"看板环境缺少 <变量名>"。看板不启动也不停止该环境。

## 浏览器独占、暂停与执行状态
<!-- tw-section id=I4 type=interface depends=scope-global,I2 -->
**浏览器独占**。所选场景中有 `surface` 为 `web` 的入口时，任务先取浏览器锁再取 Codex 名额，拿不到锁时为"等待浏览器"，拿到锁而名额已满时为"排队中"；任务结束或异常时两者都释放。锁只在看板进程内，整个看板同一时间只有一个任务持有。

**暂停**。开发者 `POST /api/v1/automation/pause` 与 `…/resume`（开发者令牌，返回 `{ok, paused}`）暂停与恢复看板的全部自动任务（合并、执行、检查，以及后续需求的修复与沉淀）；暂停状态保存在存储中，重启后保持。暂停期间排队中、待提交与等待浏览器的任务不开始，运行中的任务继续到结束；开发者手工发起的执行同样等待。首页顶部显示"自动任务已暂停"及恢复按钮；按钮的令牌提示同 SOP-BOARD-3 I2。

**执行状态**。SOP-BOARD-2 I5 的批次执行状态增加：有执行任务运行中时为"执行中（<会话数> 个会话，开始于 <本地时间>，任务 #<编号>）"，否则有待提交的执行任务时为"待提交：工作区有未提交的产品改动"，否则沿用 SOP-BOARD-2 的状态；暂停时附"已暂停"。批次总览增加"执行任务"列表（含单独发起的检查任务，种类一列区分）：编号、种类、触发原因（合并写入、场景版本或开发者发起）、范围条数、状态、开始与结束时间、作废原因或失败原因、所登记的轮次号；开发者按钮"执行待执行场景""运行回归检查"。

## 回归检查运行器
<!-- tw-section id=I5 type=interface depends=scope-global,I1,D1 -->
**触发**。执行任务开始时，看板在同一快照、同一检出目录（`in-place` 时用一个临时 worktree `worktrees/<任务编号>-check/`，任务结束时移除）上，为批次中声明了 `regression.check` 且属于下列之一的场景运行其检查：执行范围之内的；总体方案 S2 中因产品变化而待确认的。开发者 `POST …/batches/<批次>/checks`（开发者令牌）可单独发起，范围为全部声明了回归的场景，返回 `{ok, task}`；没有这类场景返回 409 `nothing-to-check`，未授权项目返回 409 `repo-not-writable`。单独发起的检查任务同样遵守待提交、在快照提交的 worktree 中运行并核对快照。同一检查在一次任务中只运行一次，覆盖引用它的全部场景。回归检查不占 Codex 名额。

**运行**。命令为声明的 `argv`（`{output}` 替换为任务目录下 `checks/<检查编号>/`），工作目录为检出目录下的 `cwd`，白名单环境加上 `required_env` 中的变量（缺任何一个时该检查记为"受阻：缺少环境变量 <名>"，不运行），超过 `timeout` 时终止进程组，该检查记为失败，引用它的场景 `actual` 写"回归检查 <检查编号> 超时（<秒> 秒）"。标准输出与错误输出写入 `checks/<检查编号>/output.txt`。

**解析**。`junit`：读取 `report`，每个 `testcase` 的用例名为 `<classname>::<name>`，有 `failure` 或 `error` 子元素为失败，有 `skipped` 为跳过，否则通过。`go-json`：逐行读取 `go test -json` 输出，`Action` 为 `pass`、`fail`、`skip` 且有 `Test` 的行，用例名为 `<Package>::<Test>`。报告缺失或无法解析时该检查记为失败并写明原因。

**结果**。对每条场景，声明的 `cases` 全部出现且通过时该场景为 `pass`（命令退出码非零也如此，`actual` 另注明退出码），`actual` 写"回归检查 <检查编号> 通过：声明的 N 个用例全部通过"；否则为 `fail`，`actual` 列出缺失、失败或跳过的用例与命令退出码；受阻时为 `blocked`。这些结果登记为一个 Agent 轮次，执行者为"回归检查 <检查编号>"，会话为任务编号，证据为该检查的 `output.txt` 与报告文件，产品为快照中的提交号与指纹。快照作废时检查结果同样不登记。

**场景列表中的检查状态**。场景列表接口（`…/scenarios`）每条场景增加 `regression_status`：声明了 `regression.check` 的场景，批次当前产品指纹上该场景最新的回归检查结果为 `pass` 时为 `passed`，为 `fail` 或 `blocked` 时为 `failed`，尚未运行（含产品指纹变化后）时为 `pending`；没有声明时为 null。页面上的"检查通过""检查失败""检查待运行"取同一字段。

## 存储
<!-- tw-section id=D1 type=data depends=scope-global -->
沿用 SOP-BOARD-3 的任务表，任务种类增加 `execute`（执行，含其回归检查）与 `check`（单独发起的回归检查）；任务行另存范围、快照摘要、作废原因与登记的轮次号。新增设置表保存暂停状态。worktree 在数据目录的 `worktrees/` 下；重启时进行中的任务按 SOP-BOARD-3 转"已中断"，随后删除 `worktrees/` 下的全部目录并在授权仓库运行 `git worktree prune`（重试会重新建 worktree）。

## 验收 A1：入口与检查声明
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1 -->
SOP-41 节点全部场景通过。

- 场景：docs/sop/sop-board-4.yaml#SOP-41
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D2、D23；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-4.md 的 I1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A2：执行任务
<!-- tw-section id=A2 type=acceptance depends=scope-global,I2,D1 -->
SOP-42 节点全部场景通过。

- 场景：docs/sop/sop-board-4.yaml#SOP-42
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D4、D9、D22 与第 4 节 S2；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-4.md 的 I2、D1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A3：候选版本
<!-- tw-section id=A3 type=acceptance depends=scope-global,I3 -->
SOP-43 节点全部场景通过。

- 场景：docs/sop/sop-board-4.yaml#SOP-43
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D22、D23；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-4.md 的 I3 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A4：浏览器独占、暂停与执行状态
<!-- tw-section id=A4 type=acceptance depends=scope-global,I4 -->
SOP-44 节点全部场景通过。

- 场景：docs/sop/sop-board-4.yaml#SOP-44
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D21、D22；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-4.md 的 I4 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A5：回归检查运行器
<!-- tw-section id=A5 type=acceptance depends=scope-global,I5,D1 -->
SOP-45 节点全部场景通过。

- 场景：docs/sop/sop-board-4.yaml#SOP-45
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D2、D29 与第 4 节 S2；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-4.md 的 I5、D1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 任务 T1：入口声明与回归检查运行器
<!-- tw-section id=T1 type=task depends=scope-global,I1,I5,D1 -->
实现 I1 与 I5（含开发者单独发起的检查任务与结果登记）。测试用临时 Git 仓库中声明的检查：一个产出 JUnit XML 的脚本与一个产出 `go test -json` 格式的脚本（不依赖本机安装 Go），覆盖通过、失败、跳过、缺失用例、缺环境变量、超时与报告缺失。先写失败测试再实现。

- 验收：A1、A5
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T2：执行任务与候选版本
<!-- tw-section id=T2 type=task depends=scope-global,I2,I3,D1 -->
在 T1 之后实施。实现 I2 与 I3：触发与范围合并、已提交代码原则与"待提交"、快照与作废、worktree 生命周期、`start` 启动与就绪、`in-place` 地址传递、执行会话的输入与结果核对、Agent 轮次登记。测试用 SOP-BOARD-3 的假 `codex`（按任务目录中的 `scenarios.yaml` 写出预设的 `results.jsonl` 与证据）与一个以 `SOP_PORT` 监听的最小 HTTP 服务。另有一项真实入口验证由验收执行者用本机已登录的 Codex 与 `checkout` 入口完成。先写失败测试再实现。

- 验收：A2、A3
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T3：浏览器独占、暂停与执行状态
<!-- tw-section id=T3 type=task depends=scope-global,I4 -->
在 T2 之后实施。实现 I4：浏览器锁与名额的获取顺序和释放、暂停与恢复及其持久化、批次执行状态与执行任务列表、开发者按钮。先写失败测试再实现。

- 验收：A4
- 范围：src、tests、README.md
- 前置任务：无
