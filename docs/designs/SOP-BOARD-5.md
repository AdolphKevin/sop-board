---
design: "D-sop-board-5"
task: "SOP-BOARD-5"
revision: 2
assessment: {"complexity":"complex","risks":["persistence","public-contract","concurrency","authorization","cross-service"],"effort":"约三个工作日代理时长","drivers":["每个授权仓库一个整体修复任务，同一会话跨多次尝试，三次未通过转开发者","复测由新会话执行，并在候选版本上运行全部回归检查","合入前后核对基线、工作区与分支，看板不暂存、不还原、不强制移动分支","in-place 入口修复后直接替换并运行 reload，复测在开发者的运行环境中进行","通过后在 Jira 写评论并流转到 JIRA_FIXED_STATUS"],"uncertainties":[],"required_skills":["Python","SQLite","子进程与并发","Git worktree 与合并","Jira REST API v3","HTML 与原生 JavaScript"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-5：修复闭环

> 本文件是 tw 任务 SOP-BOARD-5 的方案原文。总体设计见本仓 [docs/proposals/2026-10-19-standalone-sop-verification.md](../proposals/2026-10-19-standalone-sop-verification.md)（v8），本需求对应其第 8 节 SOP-BOARD-5 一行，以及 D7、D21、D24、D25、D26、D27 中属于本需求的部分。验收场景见 [docs/sop/sop-board-5.yaml](../sop/sop-board-5.yaml)。
>
> 状态：修订 2。修订 1 于 2026-10-20 经 AdolphKevin 授权由 Claude 代批；修订 2 按测试代理 sop-board-5-tester 的 21 个待确认问题与 8 处出入补写，同日代批。真实 Jira 站点的接入验证按 AdolphKevin 2026-10-19 的决定延到外部接入阶段，本需求以本机模拟 Jira 验收。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
授权仓库中出现未解决的问题时，看板为该仓库建一个整体修复任务：读取各问题的 Jira 单，在 worktree 的修复分支上由一个 Codex 会话先做根因分析再从根因修复，非产品缺陷转开发者。每次修复后由新会话复测问题场景，并在候选版本上运行全部回归检查；全部通过后按入口的候选方式合入：`checkout` 与 `start` 在 worktree 上验证后经保护快进合入，`in-place` 在回归检查通过后直接替换、运行 `reload`、在开发者的运行环境中复测。连续三次未通过转开发者，已替换的代码保留。合入后候选结果提升为批次的 Agent 轮次，Jira 写评论并流转到 `JIRA_FIXED_STATUS`。页面与报告显示修复任务的状态、记录与统计。

非目标：沉淀任务、守护与漏检（SOP-BOARD-6）；`sop-verify status`（SOP-VERIFY-3）；推送远端、部署到测试环境、建 Jira 单；以真实 Jira 站点验证（外部接入阶段）；跨批次的趋势页（SOP-BOARD-6）。

技术选择沿用 SOP-BOARD-1 至 4。Codex 经 SOP-BOARD-3 的运行器调用（首次用新会话命令，后续尝试用续接命令），执行与回归检查复用 SOP-BOARD-4 的执行会话与检查运行器，Jira 经标准库 `urllib` 调用 REST API v3。

- 需求级验收：A1、A2、A3、A4、A5、A6、A7、A8

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | 修复任务 | 读取 Jira、建 worktree 与修复分支、调用修复会话、提交 | 看板 | 授权仓库的修复分支 | sop-board | A1、A2、A3 |
| self | 复测与合入 | 新会话复测、运行回归检查、合入基线、运行 reload、提升轮次 | 看板 | 授权仓库的基线分支与批次统计 | sop-board | A4、A5、A6 |
| self | Jira | 写评论、流转状态 | 看板 | 测试岗 | sop-board | A7 |
| self | /api/v1 开发者操作与页面 | 批准方案、重试、放弃；查看修复任务、记录与报告 | 开发者、测试岗 | 看板 | sop-board | A2、A8 |

## 触发、范围与准备
<!-- tw-section id=I1 type=interface depends=scope-global,D1 -->
**问题**。授权仓库各批次中，当前内容指纹的最新结果属于下列之一的场景是未解决的问题：人工标记 `fail` 或 `blocked`（带缺陷链接）；Agent 结果 `fail`；回归检查结果 `fail`（SOP-BOARD-4 I5 由看板登记、并记有检查来源的结果行；只改执行者名称的普通结果不算）。Agent 的 `blocked` 与 `skip` 留在问题队列，不触发修复。未授权项目的问题留在问题队列。已转开发者（I2）或所在任务被放弃的问题，在其场景出现新的结果或内容变化之前不再纳入新的修复任务。

**触发与范围**。看板每 5 秒检查：某个授权仓库有未解决的问题而没有进行中的修复任务时建一个修复任务，范围是该仓库全部批次的全部未解决问题。每个仓库同一时间只有一个修复任务（状态为已合入、已放弃以外的都算进行中）；等待 Codex 名额或等待执行任务结束时为"排队中"；暂停自动任务（SOP-BOARD-4 I4）同样作用于修复任务的开始与新的尝试；范围在首次尝试开始时按当时该仓库全部未解决问题固定（任务建立后、首次尝试开始前——含排队、待提交与暂停期间——出现的问题都在其中）；首次尝试开始后出现的新问题，在下一次尝试开始时纳入，并写入尝试记录。修复任务与同一仓库的执行任务（SOP-BOARD-4）不同时运行，修复优先：已开始的执行任务运行到结束；修复任务处于排队中、分析中、修复中、复测中或待合入时，排队中的执行任务等待，修复任务进入等待开发者的状态或结束后执行任务即可开始。

**准备**。按 SOP-BOARD-4 I2 的已提交代码原则，工作区有未提交的产品改动时任务为"待提交"。开始时：基线分支为授权仓库主工作区当前所在的分支（处于分离头指针时不建 worktree，任务转"待开发者处理"，原因"主工作区不在分支上"，开发者切回分支后点"重试"重新准备）；看板在数据目录 `worktrees/fix-<任务编号>/` 以基线分支最新提交建 worktree 与分支 `sop-board/fix/<任务编号>`；固定场景、入口与用例并集的快照（同 SOP-BOARD-4 I2）；配置了 Jira 时读取每个问题缺陷链接对应 Jira 单的标题、描述、状态、评论与附件名，读取失败时在任务中记下原因，以标记内容继续。

## 修复会话
<!-- tw-section id=I2 type=interface depends=scope-global,I1 -->
同一任务的各次尝试用同一个 Codex 会话：首次用新会话命令（`-C` 为 worktree），此后用续接命令（进程工作目录为 worktree）（SOP-BOARD-3 I3 的两种启动方式，工作目录按任务种类取；修复任务取 worktree）；沙箱 `workspace-write`，可联网。任务目录中的输入文件以绝对路径写在提示词中供会话读取。看板在任务目录写入输入：`problems.json`（每个问题的批次、场景快照、最新结果的实际结果、证据路径、缺陷链接与 Jira 单内容、此前的 Agent 结果）、`entries.json`、本次尝试新增的问题与上次验证的证据。提示词随看板发行（`src/sop_board/prompts/fix.md`），要求依次：

1. 对全部问题做一份根因分析，判定每个问题属于 `product`（产品缺陷）、`execution`（执行问题）、`environment`（环境问题）、`scenario`（场景问题）、`requirement`（需求问题）或 `unfixable`（产品缺陷但会话无法修复）；
2. 只修 `product` 问题，并从根因修复：直接替换错误的逻辑，不加兼容层，不保留新旧两套实现，不做只让个别场景通过的局部修补，顾及其他场景与已有行为；
3. 按仓库自己的开发规则（AGENTS.md、CLAUDE.md 与已装技能）修复，仓库约定 TDD 时先写能复现缺陷的失败测试；
4. 场景文件（`docs/sop/` 下）与入口声明保持原样，改动留在 worktree，不提交；
5. 最终输出符合随看板发行的模式 `fix.schema.json`：`root_cause`（文字）、`problems`（每项 `batch`、`scenario`、`category`、`handling`、`reason`）、`changed_files`、`tests`（每项 `command`、`result`）、`plan`（转开发者的方案，没有时为空字符串）。

输出不符合模式或遗漏了某个问题时，本次尝试计为未通过，原因写入尝试记录。

**转开发者**。判定为 `product` 以外的问题，任务把它们记为"待开发者处理"事项（附理由与方案），不为它们改代码；同一任务的 `product` 问题照常进入提交与验证。全部问题都不是 `product` 时，任务转"待开发者处理"。开发者 `POST /api/v1/tasks/<任务编号>/approve`（开发者令牌，正文 `{note}`）批准方案后，看板用续接命令在同一会话中继续，提示词附上开发者说明，开始新的一次尝试；任务不在"待开发者处理"时返回 409 `task-state-invalid`。

## 提交与尝试计数
<!-- tw-section id=I3 type=interface depends=scope-global,I2 -->
**提交**。会话结束后，看板核对 worktree 的改动：涉及 `docs/sop/` 下的文件或所用入口声明文件时，本次尝试计为未通过，原因"修复改动涉及场景文件或入口声明"，worktree 恢复到本次尝试开始时的提交；没有改动而仍有 `product` 问题时同样计为未通过，原因"修复会话没有改动文件"。核对通过后看板在修复分支提交，提交信息首行为"sop-board 修复 #<任务编号> 第 <n> 次尝试"，正文逐行写"<批次> <场景编号> <Jira 单号>"，没有 Jira 单号时写缺陷链接，Agent 失败或检查失败的问题写"Agent 失败"或"回归检查失败"，作者取授权仓库的 `git config user.name` 与 `user.email`。提交时照常运行仓库的提交钩子（不签名）；钩子未通过时本次尝试计为未通过，原因"仓库提交钩子未通过"，钩子输出交回修复会话。

**计数**。一次修复加一次验证为一次尝试。验证未通过（问题场景复测未全部通过、候选启动失败、回归检查失败、合入前基线前进后的追加验证失败）时，看板把证据与新出现的问题交回同一会话开始下一次尝试；连续三次未通过，任务转"待开发者处理"，附每次的根因、改动、提交与复测证据，并记录会话编号。会话异常中断（非零退出、超时、没有写出最终输出）不计次数，看板重试一次（已有会话编号时用续接命令，在会话开始前中断而没有编号时用新会话命令），仍中断则任务转"待开发者处理"，原因写明中断；修复任务没有"失败"状态。

## 复测与合入（checkout 与 start）
<!-- tw-section id=I4 type=interface depends=scope-global,I3,D1 -->
**验证**。每次提交后，看板在 worktree 上：按问题场景所用入口的候选方式（SOP-BOARD-4 I3）启动候选，由一个新的 Codex 执行会话（与修复会话不同，也不复用之前的复测会话）复测任务中判定为 `product` 的全部问题场景（已转开发者的不在其中）；同时运行全部由回归声明守护的场景的检查（SOP-BOARD-4 I5）。问题场景全部为 `pass` 且检查全部通过才算验证通过。复测与检查的结果存为候选记录（I6），不进入批次统计。其余场景不自动重跑。

**合入保护**。验证通过后、合入前，看板重新核对：基线分支检出在哪个工作目录、该目录当前所在分支、基线分支的最新提交与候选提交。
- 基线已前进：在 worktree 中合入基线最新提交；有冲突时中止合入，任务转"待开发者合入"；无冲突时对合并提交再验证一次（计入本次尝试），通过后继续。
- 基线分支检出在授权仓库的某个工作目录（主工作区或 `git worktree list` 中的其他工作目录）：在该目录运行 `git merge --ff-only sop-board/fix/<任务编号>`；Git 因未提交修改拒绝时任务转"待开发者合入"，工作区保持原样；合入成功而工作区另有未提交修改时，任务记录"工作区含未经验收的修改"。
- 基线分支已不在任何工作目录检出（开发者切换了分支）：任务转"待开发者合入"，原因写明主工作区当前分支；看板不以其他方式更新基线分支。

看板不暂存、不还原、不强制移动分支，不推送。合入成功后任务为"已合入"，按 I6 提升，按 I7 收尾。

## 原地替换（in-place）
<!-- tw-section id=I5 type=interface depends=scope-global,I3,I4 -->
问题场景所用入口为 `in-place` 时，每次提交后：看板先在 worktree 上运行全部回归检查；检查失败时本次尝试未通过，交回修复会话，不替换。检查通过后按 I4 的合入保护把修复快进合入基线分支（直接替换），在授权仓库目录以白名单环境运行入口的 `reload`（未声明时跳过，依赖被测系统自身的热更新；`reload` 退出码非零时本次尝试未通过，原因附其输出，已合入的代码保留，下一次尝试以基线最新提交为起点），再由新的执行会话在开发者的运行环境中复测问题场景。复测结果直接登记为 Agent 轮次（与 I6 中只计入修复统计的候选记录不同，这是总体方案 D25 对 in-place 的规定，结果进入批次统计），附 `note`"运行环境按 HEAD 记录，未经核实"。各次尝试沿用同一修复分支名，每次从基线最新提交重新建立（看板只对 `sop-board/` 前缀的自有分支做这种移动）；in-place 时基线前进后的追加验证只重跑回归检查，复测在替换后的运行环境中进行。问题场景未全部通过时，在已替换的代码上继续下一次尝试（下一次的 worktree 以基线最新提交为起点）；三次未通过时保留已替换的代码，任务转"待开发者处理"，列出本任务合入基线的全部提交与每次的证据。看板不自动回退。

同一任务的问题既有 `in-place` 入口又有其他入口时，按 `in-place` 处理整个任务。

## 登记与提升
<!-- tw-section id=I6 type=interface depends=scope-global,I4,D1 -->
worktree 上的复测与检查结果先存为候选记录（任务、尝试、候选提交、结果行、证据）。合入后，看板把最后一次通过的候选记录提升为批次的 Agent 轮次：每个涉及的批次一个轮次，执行者为复测会话编号，实现者为修复会话编号，执行时间保留原值，产品为合入后基线的提交号与产品指纹；批次产品指纹随之更新。提升以"任务编号 + 尝试编号 + 批次"为幂等键，重复执行结果不变。看板重启时发现候选提交已在基线分支中而未提升的，补做提升。未通过的候选记录只计入修复统计。

## 收尾与 Jira 回写
<!-- tw-section id=I7 type=interface depends=scope-global,I6 -->
任务"已合入"后：问题场景因 Agent 通过回到复测队列等测试岗复核；看板删除 worktree 与已合入的修复分支（`git branch -d`）。

对任务中缺陷链接为 Jira 链接的问题，三个 Jira 变量都配置时看板为每张单：
1. 写一条评论（REST API v3 `POST /rest/api/3/issue/<单号>/comment`，正文为 Atlassian 文档格式）：根因摘要、改动文件、修复提交号与合入的本机基线分支名、复测场景与结果，并注明"尚未部署到测试环境"；
2. 环境变量 `JIRA_FIXED_STATUS` 有值时，读取该单可用的流转（`GET …/transitions`），选目标状态名与之相同的一项执行（`POST …/transitions`）；没有这样的流转时不流转，记为"Jira 状态待人工更新"。`JIRA_FIXED_STATUS` 未设置时只写评论，同样记"Jira 状态待人工更新"。

评论失败时照常尝试流转。每张单的回写结果（评论成功或失败及原因、流转成功、待人工更新）记入任务，失败的回写不自动重试，列入待开发者处理的事项（`jira-manual`）；Jira 三个变量未配齐时每张单记"Jira 未配置，未回写"。失败不影响任务"已合入"。令牌不出现在任务目录、页面、接口响应与日志中。

## 状态、记录与报告
<!-- tw-section id=I8 type=interface depends=scope-global,I1,I7,D1 -->
修复任务的状态取：排队中、待提交、分析中（首个尝试的会话运行中）、修复中（后续尝试的会话运行中）、复测中、待合入（验证通过、正在合入）、已合入、待开发者处理、待开发者合入、已中断、已放弃、Codex 不可用，均附原因。看板重启时，分析中、修复中、复测中与待合入的任务转"已中断"，等待中的状态保持。开发者操作：批准方案（I2）；重试（已中断、Codex 不可用，以及因分离头指针或两次会话中断转出的待开发者处理：先按修复分支重建 worktree，有会话编号时以续接命令继续同一会话，否则重新准备）；放弃（除已合入、已放弃与待合入外的状态都可放弃；删除 worktree 与未合入的修复分支，已合入基线的提交保留）。

页面：首页每个授权项目显示进行中的修复任务及状态；新增修复页 `/p/<项目键>/fixes`（项目级）列出修复任务：编号、问题数、状态与原因、尝试次数、会话编号；所选任务显示根因分析、每个问题的判定与处理、每次尝试的提交、复测结果与证据链接、合入提交、待开发者处理事项、Jira 回写结果，以及"批准方案""重试""放弃"按钮。批次的 SOP 场景页与场景详情页上，处于进行中修复任务的问题场景标"修复中 #<任务编号>"、已转开发者的标"待开发者处理 #<任务编号>"，并链接到修复页；任务已合入或已放弃后不再标记，场景详情页的历史中保留"所属修复任务 #<编号>"。批次总览显示该批次的自动修复统计（同报告）。未授权项目的修复页与 `fixes` 接口返回空列表。`GET /api/v1/projects/<项目键>/fixes` 与 `GET /api/v1/tasks/<任务编号>` 返回同样的内容。

**待开发者处理的事项** `GET /api/v1/projects/<项目键>/attention`（读取不需要令牌）返回 `{ok, items}`，供 `sop-verify status` 使用：每项 `{kind, task, batch, reason, url, resume}`，`kind` 取 `developer-action`（待开发者处理）、`developer-merge`（待开发者合入）、`commit-needed`（待提交）、`cases-needed`（合并待补充）、`sync-needed`（待同步）、`jira-manual`（Jira 状态待人工更新）、`interrupted`（已中断）；涵盖该项目全部任务种类（合并、执行、修复，以及后续的沉淀）；`url` 为看板上对应页面的完整地址（取请求的 Host）；`resume` 为接续该任务 Codex 会话的命令 `codex resume <会话编号>`，任务没有会话时为空字符串。

报告：SOP-BOARD-2 I5 的 Markdown 报告"修复记录"区块列出涉及该批次的修复任务（问题数只计该批次的问题，尝试次数为整个任务的）：该批次的问题场景、根因摘要、尝试次数、结果（已合入、待开发者处理等）、合入提交、Jira 回写结果；并给出自动修复统计：任务数、问题数、已合入数、转开发者数、首次尝试即通过的任务数与总尝试次数。没有修复任务时仍写"暂无记录"。

## 存储
<!-- tw-section id=D1 type=data depends=scope-global -->
任务表增加种类 `fix`，另存仓库、基线分支、修复分支、worktree、会话编号、尝试次数与当前原因。新增表：修复问题（任务、批次、场景、内容指纹、判定、处理、理由、纳入的尝试序号）；尝试（任务、序号、开始与结束时间、提交、结果、原因、复测会话编号）；候选记录（任务、尝试、批次、候选提交、结果行 JSON、证据摘要、是否已提升、提升的轮次号）；Jira 回写（任务、单号、评论结果、流转结果、原因）。

## 验收 A1：触发、范围与准备
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1 -->
SOP-51 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-51
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D21、D22、D24；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A2：修复会话与转开发者
<!-- tw-section id=A2 type=acceptance depends=scope-global,I2 -->
SOP-52 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-52
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D24；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A3：提交与尝试计数
<!-- tw-section id=A3 type=acceptance depends=scope-global,I3 -->
SOP-53 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-53
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D24；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I3 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A4：复测与合入保护
<!-- tw-section id=A4 type=acceptance depends=scope-global,I4 -->
SOP-54 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-54
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D25；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I4 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A5：原地替换
<!-- tw-section id=A5 type=acceptance depends=scope-global,I5 -->
SOP-55 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-55
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D23、D25；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I5 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A6：登记与提升
<!-- tw-section id=A6 type=acceptance depends=scope-global,I6,D1 -->
SOP-56 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-56
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D25；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I6、D1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A7：收尾与 Jira 回写
<!-- tw-section id=A7 type=acceptance depends=scope-global,I7 -->
SOP-57 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-57
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D7、D25；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I7 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A8：状态、记录与报告
<!-- tw-section id=A8 type=acceptance depends=scope-global,I8 -->
SOP-58 节点全部场景通过。

- 场景：docs/sop/sop-board-5.yaml#SOP-58
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D26、D27；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-5.md 的 I8 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 任务 T1：修复任务、会话与提交
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2,I3,D1 -->
实现 I1、I2、I3 与 D1 中修复问题与尝试的存储：问题识别、触发与范围、与执行任务的互斥、准备（基线分支、worktree 与修复分支、快照、Jira 读取）、修复会话的输入、提示词与输出模式、转开发者与批准、提交核对与提交、尝试计数与会话中断重试。测试用 SOP-BOARD-3 的假 `codex`（按预设在 worktree 中改文件并写出最终输出，可模拟各种判定、遗漏问题、改动场景文件与中断）、临时 Git 仓库与本机模拟 Jira。先写失败测试再实现。

- 验收：A1、A2、A3
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T2：复测、合入保护与提升
<!-- tw-section id=T2 type=task depends=scope-global,I4,I6,D1 -->
在 T1 之后实施。实现 I4 与 I6：新会话复测与全部回归检查、候选记录、基线前进时的合入与追加验证、四种合入情形、提升与幂等、重启补做提升。测试用临时 Git 仓库构造基线前进、冲突、工作区未提交修改与切换分支等情形。先写失败测试再实现。

- 验收：A4、A6
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T3：原地替换
<!-- tw-section id=T3 type=task depends=scope-global,I5 -->
在 T2 之后实施。实现 I5：检查先行、直接替换、`reload`、在运行环境中复测与直接登记、在已替换代码上继续尝试、三次未通过时保留代码并列出提交。测试用一个按工作区文件内容响应的最小 HTTP 服务作为开发者的运行环境。先写失败测试再实现。

- 验收：A5
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T4：Jira 回写、状态与报告
<!-- tw-section id=T4 type=task depends=scope-global,I7,I8 -->
在 T3 之后实施。实现 I7 与 I8：收尾清理、Jira 评论与流转（模拟 Jira 核对请求、认证头、文档格式正文与流转选择）、修复页、SOP 场景页标记、任务接口、报告的修复记录区块与统计。先写失败测试再实现。

- 验收：A7、A8
- 范围：src、tests、README.md
- 前置任务：无
