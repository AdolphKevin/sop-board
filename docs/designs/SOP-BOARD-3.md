---
design: "D-sop-board-3"
task: "SOP-BOARD-3"
revision: 1
assessment: {"complexity":"complex","risks":["persistence","public-contract","concurrency","authorization"],"effort":"约两个半工作日代理时长","drivers":["看板在本机启动 Codex 进程并管理队列、任务目录与原始输出","开发者授权后看板写入项目仓库工作区，授权与开发者令牌决定写操作的边界","以用例为准的合并由看板确定性生成用例场景，Codex 只回答需要判断的字段","SOP 原文与版本页渲染 Markdown 与 mermaid，不依赖外网"],"uncertainties":[],"required_skills":["Python","SQLite","HTTP","子进程与并发","Git","HTML 与原生 JavaScript"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-3：Codex 任务、可写授权与以用例为准的合并

> 本文件是 tw 任务 SOP-BOARD-3 的方案原文。总体设计见本仓 [docs/proposals/2026-10-19-standalone-sop-verification.md](../proposals/2026-10-19-standalone-sop-verification.md)（v8），本需求对应其第 8 节 SOP-BOARD-3 一行，以及 D15、D16、D17、D20、D21 与 S7 中属于本需求的部分。验收场景见 [docs/sop/sop-board-3.yaml](../sop/sop-board-3.yaml)。
>
> 状态：修订 1，2026-10-19 经 AdolphKevin 授权由 Claude 代批。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
看板在本机调用已登录的 Codex 完成任务：任务进入队列，同时运行的 Codex 进程不超过三个，每个任务有自己的目录并保留原始输出。开发者用 `--writable-repo` 授权仓库并设置开发者令牌后，看板发现该仓库 `docs/sop/` 下的场景文件，把合并结果写入工作区；未授权时看板只生成草稿。测试岗上传用例或场景文件变化后，看板以用例为准合并：用例生成的场景由看板按 S7 确定性生成，Codex 只回答角色、入口、反向类型、节点分组与原有 SOP 场景的去向；看板核对后写入或存为草稿。看板展示 SOP 原文与版本。

非目标：看板发起的执行与回归检查（SOP-BOARD-4）；修复闭环（SOP-BOARD-5）；场景沉淀（SOP-BOARD-6）；sop-verify 的看板客户端（SOP-VERIFY-2）。

技术选择沿用 SOP-BOARD-1、2。Codex 以子进程调用本机 `codex` 命令；mermaid 脚本随仓库提供（`src/sop_board/assets/vendor/`，保留其许可文件），页面不加载外网资源。

- 需求级验收：A1、A2、A3、A4、A5

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | uv run sop-board --writable-repo | 授权仓库、登记项目、发现场景文件 | 启动看板的开发者 | 看板 | sop-board | A1 |
| self | /api/v1 开发者操作 | 重新合并、重试与放弃任务 | 开发者页面 | 看板任务队列 | sop-board | A1、A2 |
| self | 合并页、任务读取接口 | 查看合并结果、对应关系与原始输出；下载草稿与配对 | 看板 | 测试岗、开发岗、sop-verify | sop-board | A2、A3、A4 |
| self | SOP 页与推送接口 | 推送与浏览 SOP 原文、版本与差异 | sop-verify 与授权仓库 | 测试岗、开发岗 | sop-board | A5 |

## 启动与可写授权
<!-- tw-section id=I1 type=interface depends=scope-global -->
启动参数增加 `--writable-repo <项目身份>=<本机仓库路径>`，可重复。给出任意一项时，环境变量 `SOP_BOARD_ADMIN_TOKEN` 必须非空且不同于 `SOP_BOARD_TOKEN`；每个路径必须是 Git 工作区的根目录。不满足时以退出码 1 结束，错误输出写明原因（缺开发者令牌、两个令牌相同、路径不是 Git 工作区根目录并给出路径），并按 SOP-BOARD-1 的规则不留下新文件。

启动成功时，每个授权仓库在标准输出写一行 `可写授权：<项目身份>（<路径>）`；项目不存在时以该身份登记，名称取仓库目录名。首页与批次页面对授权项目显示"可写"，其他项目显示"只读"。去掉参数重启即撤销授权；重启时状态为排队中或运行中的任务一律转"已中断"。

**场景文件发现**。看板启动时与此后每 5 秒读取各授权仓库 `docs/sop/` 下除 `entries.yaml` 外的 `*.yaml`：文件合格（按 sop-verify 的场景文件规则）且 `metadata.batch` 对应的批次没有该内容的版本时，登记为新的场景版本，推送人写"工作区"，提交号取仓库 HEAD，`dirty` 按工作区是否有未提交改动，产品指纹按 D2 计算；同一内容只登记一次；看板自己写入的内容不再登记。文件不合格时批次页面显示"工作区场景文件不合格"及问题与行号，已有版本保持不变。

## 开发者令牌与开发者操作
<!-- tw-section id=I2 type=interface depends=scope-global,I1 -->
开发者操作凭 `Authorization: Bearer <SOP_BOARD_ADMIN_TOKEN>`；缺失返回 401 `unauthorized`，使用写入令牌返回 403 `admin-required`。本需求的开发者操作：

- `POST /api/v1/projects/<项目键>/batches/<批次>/merges`：发起重新合并，返回 `{ok, task}`；与已有排队或运行中的合并任务输入相同时返回该任务。
- `POST /api/v1/tasks/<任务编号>/retry`：对状态为失败、已中断或 Codex 不可用的任务以相同输入重新排队。
- `POST /api/v1/tasks/<任务编号>/cancel`：放弃排队中、运行中、已中断或待同步的任务；运行中的 Codex 进程组被终止。

页面身份栏增加"开发者令牌"栏，保存在浏览器 `localStorage` 的 `sop-board.admin-token`；开发者操作按钮带上该令牌，未填写时提示填写。写入令牌的用法不变。

## Codex 运行器与任务
<!-- tw-section id=I3 type=interface depends=scope-global,D1 -->
每个任务有编号、种类（本需求只有 `merge`）、项目、批次、状态、触发原因、输入摘要、创建与开始与结束时间、Codex 会话编号与错误说明。状态取：排队中、运行中、已完成、失败、已中断、待同步、待补充、已放弃、Codex 不可用。

**运行**。同时运行的 Codex 进程不超过三个，其余任务按创建顺序排队；等待人工的任务不占名额。每个任务的目录为 `<数据目录>/tasks/<任务编号>/`，存放输入快照、`prompt.md`、`schema.json`、`events.jsonl`（Codex 的 `--json` 输出）、`stderr.txt`、`final.json`（最终消息）与 `check.json`（看板核对结果）。新会话命令为：

```text
codex exec --json -s <沙箱> --skip-git-repo-check -C <任务目录> --output-schema <任务目录>/schema.json -o <任务目录>/final.json <提示词>
```

合并任务的沙箱为 `read-only`。标准输入接空设备；会话编号取 `events.jsonl` 中 `thread.started` 事件的 `thread_id`。子进程环境只保留白名单变量（`PATH`、`HOME`、`USER`、`LANG`、`LC_*`、`TMPDIR`、`CODEX_HOME`、`SHELL`），不含 `SOP_BOARD_TOKEN`、`SOP_BOARD_ADMIN_TOKEN` 与 `JIRA_*`。单个任务运行超过环境变量 `SOP_BOARD_CODEX_TIMEOUT` 指定的秒数（缺省 1800）时终止其进程组，任务转"失败"并写明超时。

**Codex 不可用**。启动时与每次取任务前检查 `codex --version`；命令不存在或失败时任务停在"Codex 不可用"，页面写明原因；恢复后由开发者重试。

**读取**。`GET /api/v1/tasks?project=<项目键>&batch=<批次>` 列出任务；`GET /api/v1/tasks/<任务编号>` 返回任务字段与目录中各文件的下载地址；`GET /api/v1/tasks/<任务编号>/files/<文件名>` 下载目录中的文件。读取不需要令牌。

## 以用例为准的合并
<!-- tw-section id=I4 type=interface depends=scope-global,I3,D1,D2 -->
**触发**。批次有用例时：上传用例（SOP-BOARD-2 I3）；场景文件出现新版本（推送或 I1 的发现）；开发者发起重新合并。输入摘要为用例并集版本、当前场景版本原文 sha256、SOP 原文摘要与入口声明摘要的组合：与最近一个已完成或排队、运行中的合并任务相同，或场景版本是看板自己写入的，都不再建任务。批次没有用例时不合并。

**输入快照**（任务目录）：`cases.yaml`（用例并集）、`scenarios.yaml`（当前场景文件，没有时为空）、`entries.yaml`（授权仓库取 `docs/sop/entries.yaml`，没有时取 `.tw/config.json` 的 `acceptance_entries`；未授权项目取推送附带的入口声明，见 I5）、`sop/`（SOP 原文，有则带上）、`nodes.json`（看板分配的节点编号）。

**看板生成的确定字段**（按 v8 S7）：每条用例生成一条场景，编号、标题、优先级、`tags` 取用例的同名字段，`refs` 为用例编号，极性按用例 `tags` 是否含 `negative`，`initial_state`、`steps`、`expected` 分别取用例的 `preconditions`、`steps`、`expected_results`（按解析后的字符串原样），`forbidden_effects` 为空数组。节点按用例 `tags` 中的 `section:` 值分组，节点编号由看板分配为 `S01`、`S02`……，同一批次中同一 section 的编号保持不变。

**Codex 回答的判断字段**，`final.json` 须符合随看板发行的模式：

- `cases`：每条用例的 `actor`、`verify`（取自入口声明的入口名）；反向用例的 `negative_type`（取枚举值，没有合适的取 `other`）；用例没有 `preconditions` 时补写 `initial_state`。
- `nodes`：每个 section 的节点 `title`、`actor`、`entry`；没有正向用例的 section 可写 `merge_into` 指向另一个 section；仍没有正向依据时写入 `gaps`。
- `scenarios`：原有每条非用例场景的去向：`kept`（保留，编号与内容不变）、`duplicate`（与某条用例场景重复，写 `target` 与理由，并给出需要并入该用例场景的 `forbidden_effects` 与是否转移回归声明）、`conflict`（上下文相同而预期与某条用例相反，写 `target` 与理由）。只有角色、初始状态、入口与全部结果保证都被覆盖时才可判为 `duplicate`。
- `negative_exempt`：没有反向场景的节点的豁免理由；依据出自推断的标 `inferred: true`。
- `notes`：其他说明。

**组装与核对**。看板用这些字段组装合并后的场景文件：用例节点在前，保留的原有场景留在原节点；原节点没有正向场景时，按 Codex 给出的 `relocate`（原有场景编号到用例节点编号）移入用例节点。推断出的内容（补写的 `initial_state`、`inferred` 的豁免理由）在对应位置写 `# 待确认 <编号>：<内容>` 注释。核对：场景文件格式（sop-verify 规则）；每条用例都有同编号场景且确定字段与用例一致；每条原有场景都有去向；`target`、`relocate` 引用的编号存在；原有场景没有 `TC_` 形式的编号。核对失败时任务为"失败"，`check.json` 列出问题；存在 `gaps` 时任务为"待补充"并列出缺口。重新合并时，已有场景的 `regression` 按编号保留。

**落地**。核对通过后：未授权项目存为草稿，任务"已完成"；授权项目在磁盘上的 `docs/sop/<批次>.yaml` 与合并依据的版本相同（或文件不存在）时写入该文件，入口声明缺失时以 Codex 推断的入口写 `docs/sop/entries.yaml`（文件头注释写明待确认），登记为新的场景版本（推送人"看板合并 #<任务编号>"），任务"已完成"；磁盘文件不同则任务"待同步"，文件保持原样。写入不提交。

**合并页** `/p/<项目键>/b/<批次>/merges`（批次导航新增"合并"）：任务列表（编号、触发原因、状态、时间）；所选任务的对应关系（用例到场景）、原有场景的去向与理由、待确认的推断、缺口；失败时显示 `check.json` 与原始输出的下载链接；开发者按钮"重新合并""重试""放弃"。

**下载**：`GET …/merges/<任务编号>/scenario-file`（合并结果原文）；`GET …/merges/<任务编号>/pairs.json`（配对文件，全部用例归入 `covered`，可直接用于 `sop-verify coverage --pairs`）。

## 推送附带的入口声明与 SOP 原文
<!-- tw-section id=I5 type=interface depends=scope-global,D1 -->
推送场景文件的正文增加两个可选字段：`entries`（`entries.yaml` 的内容对象，字段同 sop-verify 的入口声明）与 `sop_sources`（数组，每项 `{ref, path, commit, sha256, confirmed_by, content}`，`content` 为 Markdown 原文，不超过 2 MiB）。不合格时返回 422 `scenario-file-invalid` 并点名字段。授权仓库中，看板对场景文件 `metadata.sources` 的每项 `ref`，取其中第一个以空格分隔、指向仓库内已有文件的相对路径，读取该文件作为 SOP 原文。

**SOP 页** `/p/<项目键>/b/<批次>/sop`（批次导航新增"SOP"）：每份原文显示最新版本的渲染结果：标题、段落、列表、表格、代码块、行内代码、强调与 `http`/`https` 链接，原文中的 HTML 一律转义显示；`mermaid` 代码块由随仓库提供的 mermaid 脚本渲染成图。每个标题带锚点。版本列表写版本号、提交号、sha256 前 12 位、确认人与本地时间；选两个版本显示逐行差异。场景详情页中，场景 `refs` 的某项与 SOP 标题文字相同时链接到该锚点。

## 存储与产品指纹
<!-- tw-section id=D1 type=data depends=scope-global -->
新增三张表，启动时补建：任务（字段见 I3，另存输入摘要与结果摘要）；节点编号（批次、section 值、节点编号）；SOP 原文版本（批次、路径、版本号、`ref`、提交号、sha256、确认人、原文、登记时间）。合并草稿与对应关系存于任务目录与任务行的结果摘要。

## 产品指纹
<!-- tw-section id=D2 type=data depends=scope-global -->
授权仓库的产品指纹与 sop-verify（SOP-VERIFY-1 方案 D2）的算法相同：`git ls-files -z --cached --others --exclude-standard` 列出的文件，排除 `docs/sop/` 目录、当前场景文件与所用入口文件；按路径排序，每个文件取路径与内容 sha256，已删除的已跟踪文件记为删除，序列化后取 sha256。测试中用 sop-verify 的实现对同一仓库计算，结果相同。

## 验收 A1：可写授权与开发者令牌
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1,I2 -->
SOP-31 节点全部场景通过。

- 场景：docs/sop/sop-board-3.yaml#SOP-31
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D20、D21；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-3.md 的 I1、I2 节；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A2：Codex 运行器
<!-- tw-section id=A2 type=acceptance depends=scope-global,I3 -->
SOP-32 节点全部场景通过。

- 场景：docs/sop/sop-board-3.yaml#SOP-32
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D17、D20、D21；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-3.md 的 I3 节；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A3：合并规则
<!-- tw-section id=A3 type=acceptance depends=scope-global,I4 -->
SOP-33 节点全部场景通过。

- 场景：docs/sop/sop-board-3.yaml#SOP-33
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D16 与第 4 节 S7；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-3.md 的 I4 节；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A4：草稿、写入与待同步
<!-- tw-section id=A4 type=acceptance depends=scope-global,I1,I4,D2 -->
SOP-34 节点全部场景通过。

- 场景：docs/sop/sop-board-3.yaml#SOP-34
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D16、D20；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-3.md 的 I1、I4、D2 节；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A5：SOP 原文与版本
<!-- tw-section id=A5 type=acceptance depends=scope-global,I5 -->
SOP-35 节点全部场景通过。

- 场景：docs/sop/sop-board-3.yaml#SOP-35
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D15；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-3.md 的 I5 节；版本 修订 1；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 任务 T1：授权、开发者令牌、运行器与产品指纹
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2,I3,D1,D2 -->
实现 I1（含场景文件发现）、I2、I3 与 D2。Codex 调用经一个可替换的启动函数，测试用本机临时目录中的假 `codex` 可执行文件（按参数写出 `events.jsonl` 与 `final.json`、可模拟慢、失败与卡住）核对命令参数、环境白名单、标准输入、并发上限、排队、超时、重启中断与不可用；另有一项真实入口验证由验收执行者用本机已登录的 Codex 完成。先写失败测试再实现。

- 验收：A1、A2
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T2：合并
<!-- tw-section id=T2 type=task depends=scope-global,I3,I4,D1 -->
在 T1 之后实施。实现 I4：输入快照、提示词模板与输出模式（随包发行，放在 `src/sop_board/prompts/`）、确定字段生成、组装、核对、待补充、草稿与写入、待同步、合并页与下载。测试用假 `codex` 返回预设判断，核对组装结果与各种失败；先写失败测试再实现。

- 验收：A3、A4
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T3：SOP 原文与版本
<!-- tw-section id=T3 type=task depends=scope-global,I5,D1 -->
在 T2 之后实施。实现 I5：推送字段、授权仓库读取原文、版本、Markdown 渲染与转义、mermaid 本地脚本、差异与锚点链接。先写失败测试再实现。

- 验收：A5
- 范围：src、tests、README.md
- 前置任务：无
