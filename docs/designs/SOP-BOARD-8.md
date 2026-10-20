---
design: "D-sop-board-8"
task: "SOP-BOARD-8"
revision: 2
assessment: {"complexity":"medium","risks":["persistence","public-contract","authorization"],"effort":"约一个工作日代理时长，分两段","drivers":["团队成员的项目目录分散，工作区不能要求一个外层目录","搭建代理写数据目录被 Codex 沙箱拦住","执行依赖开发者个人的通用驱动技能，其他成员自检不通过","看板只有增量执行，没有一键全量验证"],"uncertainties":[],"required_skills":["Python","SQLite","HTML 与原生 JavaScript","Codex CLI 沙箱与 --add-dir"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-8：分散仓库的工作区、项目验证技能与一键验证

> 本文件是 tw 任务 SOP-BOARD-8 的方案原文，修订 [v9 方案](../proposals/2026-10-20-v9-workspace.md) 的 D30（工作区）与 D33（驱动技能），新增 D37–D39。两处说法不同时以本文为准。验收只以场景经真实入口执行为准，开发中不写单元、接口与集成测试。
>
> 状态：修订 2。修订 1 于 2026-10-20 经 AdolphKevin 批准，批准前确认四项取舍：增量与全量两种验证、仅本机开发者发起、验证技能放看板数据目录、工作区为名称加任意位置的仓库。修订 2 去掉 T2 对 T1 的前置依赖：SOP-82-N01 的预期含 T2 的两个按钮，两段在 T2 完成后一并验收；T1 的其余场景可先验收。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
三处现状促成本任务：（1）工作区必须是一个外层目录，仓库只能写相对路径，目录分散的成员无法搭建；（2）搭建代理要写的 `~/.sop-board/<工作区>/` 不在 Codex 会话的工作目录内，默认沙箱拒绝写入；（3）执行按入口使用 `~/.agents/skills` 下的通用驱动技能，其他成员机器上没有，自检第 5 步不通过，执行按钮一直停用；按钮也只在批次概览页、只跑增量。

本任务把工作区改为"名称加开发者指定的仓库目录"，用 `sop-board init` 登记并给出数据目录为工作目录的 Codex 会话命令；搭建时由代理按项目实际情况写出项目验证技能，执行和复测都按它操作；页面提供"执行待执行场景"和"重新验证全部场景"两个按钮。

已核实的 Codex 沙箱事实（codex-cli 0.163.0，`codex sandbox -c sandbox_mode="workspace-write"`）：读取任意目录成功；写工作目录成功；写 `~` 下其他位置被拒（Operation not permitted）；本机端口可以监听。因此分散的仓库无需加入可写目录即可读取，真正受限的是写数据目录。

非目标：页面上浏览或选择文件夹；页面上发起搭建；内网发起执行（D31 不变）；按场景勾选执行；v9 数据库数据迁移（见 Q1）；修复流程本身的变化。

- 需求级验收：A1、A2、A3

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | `sop-board init`、`sop-board [名称]`、`sop-board check [名称]`、`sop-board guide` | 登记工作区、搭建、自检、启动 | 开发者与其代理 | 看板 | sop-board | A1、A2 |
| self | 本机页面与 `POST …/executions` | 发起增量或全量验证 | 开发者 | 看板、执行会话 | sop-board | A3 |

## 工作区：名称加任意位置的仓库（D37，修订 D30）
<!-- tw-section id=I1 type=interface depends=scope-global -->
工作区由名称标识，包含开发者指定的若干 Git 仓库，仓库位置不限，不再要求共同的外层目录。名称为字母、数字、`.`、`_`、`-`，以字母或数字开头，最长 64 字符；它就是页面与接口中的工作区键。数据目录缺省 `~/.sop-board/<名称>/`，`--data` 可另指定，规则不变（不能位于任何已声明仓库内）。

命令：

- `sop-board init <名称> <仓库目录>... [--data <目录>]`：逐个核对目录是 Git 工作树根目录且有可规范化的 origin，任一不符时不创建任何文件、退出 1 并逐个说明原因；数据目录已存在时拒绝。通过后创建数据目录、写 `workspace.json`（`{"format":"sop-board-workspace-v10","name":<名称>}`）和只含 `repos` 的 `config.yaml`（别名取目录名转小写、非法字符改连字符，重复时追加序号，路径写绝对路径），然后打印数据目录、各仓库别名与身份，以及继续搭建的会话命令：`codex -C <数据目录> "按 sop-board guide 继续为工作区 <名称> 搭建"`；Claude Code 用户在当前会话执行 `/add-dir <数据目录>` 后继续。
- `sop-board [名称]`、`sop-board check [名称]`：按名称取 `~/.sop-board/<名称>/`；`--data` 时读取该目录的登记名称。省略名称时，取当前目录所在 Git 仓库的根目录，在 `~/.sop-board/*/config.yaml` 中查找 `repos` 含该目录的工作区：恰好一个则使用；没有时退出 1，提示 `sop-board init`；多个时退出 1，列出各名称请开发者指定。
- 位置参数不再是工作区目录。`workspace.json` 不是 v10 格式时拒绝读取，提示用 `sop-board init` 新建（Q1：不升级）。旧数据目录不读取、不迁移；搭建指引说明，开发者指出旧数据目录时，代理可阅读其中的适配脚本，经开发者确认后在新数据目录重写，旧库中的用例与轮次不带入。

`config.yaml` 字段变化：`repos` 的值为绝对路径或 `~/` 开头的路径，相对路径报"须为绝对路径"；同一仓库不能重复声明，规则不变。`scenarios` 改为对象 `{repo: <别名>, path: <仓内相对目录>}`，目录不能等于仓库根目录。场景文件来源 `metadata.sources[].ref` 中的路径按场景所在仓库解析，`<别名>:<仓内路径>` 指向其他仓库。批次与版本记录的 `path` 显示为 `<别名>:<仓内路径>`。

Codex 会话的目录权限：搭建会话以数据目录为工作目录，仓库保持只读，沙箱本身保证搭建不写产品仓库；`uv tool install` 与 `sop-board check` 需在沙箱外运行，由代理申请批准。执行会话保持只读读取仓库，不加 `--add-dir`；修复会话沿用现有做法，对已核对的修复工作树加 `--add-dir`。

## 项目验证技能（D38，修订 D33 的驱动技能）
<!-- tw-section id=I2 type=interface depends=scope-global,I1 -->
`config.yaml` 新增必填字段 `skill`：技能目录名，指向数据目录 `skills/<skill>/SKILL.md`。缺字段、目录名不合格或文件不存在时工作区为"尚未搭建"，问题写"验证技能不存在"，执行与修复停用。`SKILL.md` 须有 `name`、`description` 文件头，并为每个声明的入口写一个二级标题，标题含入口名；缺少的入口列为配置问题。

搭建代理在查清环境后编写此技能，内容来自对实际服务的调查并与开发者确认：每个入口怎样以最终用户身份操作（工具、地址变量）、登录方式与由谁登录、测试账号与数据的位置（只写位置和环境变量名，不写值）、结果在哪里观察（页面、接口、数据库表及只读查询方式）、证据怎样保存、数据怎样隔离与清理、已知前置条件与易错点。可以引用技能目录内的脚本或参考文件。搭建报告单独列出技能内容摘要，请开发者确认。

`entries.<名>.driver` 改为可选的辅助技能：声明时在数据目录 `skills/` 与 `SOP_BOARD_SKILLS_DIR`（缺省 `~/.agents/skills`）中依次查找，均不存在时为配置问题。删除按 surface 缺省使用 playwright-acceptance、verify-backend-sop-db、build-scenario-validation-loop 的规则；未声明 driver 的入口只按项目验证技能执行。

自检第 5 步改名"验证技能"：核对项目验证技能存在、文件头齐全、每个入口有对应标题，以及声明的辅助技能存在；消息列出技能路径与各入口。执行提示（`prompts/execute.md`）和复测提示（`prompts/fix-verify.md`）改为先读取并遵循 `<数据目录>/skills/<skill>/SKILL.md`，再按入口声明使用辅助技能。

执行与复测的快照增加 `skill_digest`：技能目录内全部文件的相对路径与内容摘要。会话结束时摘要变化则本轮作废，原因写"验证技能变化"，不登记结果。技能变化不使已有结果失效，也不要求重新自检；报告的轮次信息记录本轮技能摘要的前 12 位。

## 一键验证（D39）
<!-- tw-section id=I3 type=interface depends=scope-global,I2 -->
`POST …/batches/<批次>/executions` 接受正文 `{"scope":"pending"}`（缺省，与现状相同）或 `{"scope":"all"}`；其他值返回 422 `invalid-scope`。仅本机（D31 不变），未就绪 409 `workspace-not-ready`，没有场景 409 `nothing-to-execute`。

`all` 把当前版本的全部场景加入范围，原因记为 `rerun`；`rerun` 的场景不得沿用，结果必须实际执行并有证据，否则本轮不合格。已有排队中的同批次执行任务时合并范围，同一场景以 `rerun` 为准。`pending` 没有待执行场景时仍返回 409 `nothing-to-execute`，页面提示可改用"重新验证全部场景"。

本机页面在批次概览页、首页的每个批次条目和"SOP 场景"页显示两个按钮："执行待执行场景（N 条）"和"重新验证全部场景（M 条）"，N、M 由服务端计算。未就绪、没有场景或没有待执行时按钮停用，旁边写明原因（"尚未搭建：<首个问题>"、"自检未通过"、"尚无场景"、"没有待执行场景"）。点击后就地显示任务编号与状态，不跳转。内网页面不渲染这两个按钮。

## 验收 A1：分散仓库的工作区
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1 -->
SOP-81 节点全部场景通过；受影响的 SOP-BOARD-7 场景按更新后的命令重跑或写明理由沿用。

- 场景：docs/sop/sop-board-8.yaml#SOP-81
- 依据：位置 本文 D37；版本 修订 1（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A2：项目验证技能
<!-- tw-section id=A2 type=acceptance depends=scope-global,I2 -->
SOP-82 节点全部场景通过。

- 场景：docs/sop/sop-board-8.yaml#SOP-82
- 依据：位置 本文 D38；版本 修订 1（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A3：一键验证
<!-- tw-section id=A3 type=acceptance depends=scope-global,I3 -->
SOP-83 节点全部场景通过。

- 场景：docs/sop/sop-board-8.yaml#SOP-83
- 依据：位置 本文 D39；版本 修订 1（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 任务 T1：分散仓库的搭建与项目验证技能
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2 -->
第一段，打通最薄的端到端路径。起点：开发者在仓库 A 的 Codex 会话里请代理安装并搭建，给出另一处的仓库 B；终点：开发者在本机页面点现有的"执行待执行场景"，执行会话按项目验证技能操作，场景页显示结果与证据。实现 I1、I2；更新 `guide.md`（第 1 步改为询问名称与仓库目录并运行 init，新增"编写项目验证技能"一步，第 4 步配置示例，第 6 步自检第 5 步）、README、`docs/verification.md`，把 `docs/sop/sop-board-7.yaml` 中使用工作区目录参数的步骤改为新命令（版本 4）。

- 验收：A1、A2
- 范围：src、README.md、docs/designs、docs/sop、docs/verification.md、docs/proposals
- 前置任务：无

## 任务 T2：一键验证按钮
<!-- tw-section id=T2 type=task depends=scope-global,I3 -->
第二段。起点：所有场景已通过、没有待执行的批次；终点：开发者在首页批次条目点"重新验证全部场景"，新轮次包含全部场景、沿用为 0，页面就地显示任务状态。实现 I3，在 T1 的代码上进行，可在 T1 验收前开始；SOP-82-N01 中两个按钮的停用与原因在本段完成后验收。

- 验收：A3
- 范围：src、README.md、docs/verification.md
- 前置任务：无

## 已知问题
<!-- tw-issues -->

| 编号 | 问题 | 负责人 | 影响章节 | 阻塞阶段 | 状态 | 原因 | 证据 | 确认人 | 确认依据 | 后续任务 | 范围调整 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q1 | v9 数据目录（按目录登记、相对路径配置）是否自动升级 | AdolphKevin | I1 | implementation | accepted | 不升级：用 init 新建，代理参照旧适配脚本重写，旧库数据不带入 | 2026-10-20 会话中 AdolphKevin 的答复 | AdolphKevin | 会话答复“不升级，用 init 新建，代理参照旧的适配脚本重写” | — | — |
