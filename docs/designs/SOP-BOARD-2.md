---
design: "D-sop-board-2"
task: "SOP-BOARD-2"
revision: 2
assessment: {"complexity":"complex","risks":["persistence","public-contract"],"effort":"约两个半工作日代理时长","drivers":["人工复核改为在 SOP 场景页就地完成，标记接口按新字段改写","测试岗上传的用例按其 Schema 核对并计算并集，用例可以单独建立批次","缺陷链接改按 Jira 规范化，并在页面显示单据标题与状态","总览、进度、报告与测试岗结果文件读写同一份数据，页面轮询刷新"],"uncertainties":[],"required_skills":["Python","SQLite","HTTP","HTML 与原生 JavaScript","PyYAML"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-2：人工复核改版与用例协作

> 本文件是 tw 任务 SOP-BOARD-2 的方案原文。总体设计见本仓 [docs/proposals/2026-10-19-standalone-sop-verification.md](../proposals/2026-10-19-standalone-sop-verification.md)（v8），本需求对应其第 8 节 SOP-BOARD-2 一行，以及 D6、D7、D13、D18、D26、D27 中属于本需求的部分。验收场景见 [docs/sop/sop-board-2.yaml](../sop/sop-board-2.yaml)。
>
> 状态：修订 2。修订 1 于 2026-10-19 经 AdolphKevin 授权由 Claude 代批；修订 2 答复测试代理 sop-board-2-tester 的 22 个待确认问题（显示用语、错误代码、空值与边界写法），同日代批。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
在 SOP-BOARD-1 的看板上完成测试岗的协作面：测试同事在 SOP 场景页上浏览批次的全部场景并就地复核，"通过"一键提交，"失败""受阻"只填缺陷链接、可选填备注；上传测试用例 YAML，看板按测试岗 Schema 核对并计算并集，只有用例也能建立批次；缺陷链接按 Jira 规范化，配置 Jira 令牌时在页面上显示单据标题与状态；总览显示 Agent 执行状态，新增进度页，批次页面轮询刷新；导出 Markdown 报告；导入导出测试岗结果 Schema 的 `results.yaml`。

本需求取代 SOP-BOARD-1 方案 I2 中的人工标记接口与 A4：`docs/sop/sop-board.yaml` 删除 SOP-04 节点（版本 5），人工复核改由本需求的 SOP-21、SOP-22 节点覆盖；受影响的 SOP-05、SOP-06、SOP-07 场景按新接口改写，由验收 A7 重新覆盖；SOP-BOARD-1 中使用旧标记接口的测试按 I2 改写。另按 SOP-BOARD-1 第 5 轮验收的观察修改其 I1 的一处说明：数据目录指向已存在的普通文件时，错误输出写明该路径不是目录（`docs/sop/sop-board.yaml` 的 SOP-01-N02 相应改写，同由 A7 覆盖）。

非目标：以真实 Jira 站点验证读取（AdolphKevin 2026-10-19 决定先跑通全流程，外部接入另行验证，本需求以模拟 Jira 验收）；用例与场景的合并、Codex 任务与可写授权（SOP-BOARD-3）；SOP 原文页（SOP-BOARD-3）；看板发起的执行与回归检查（SOP-BOARD-4）；修复闭环与 Jira 回写（SOP-BOARD-5）；场景沉淀（SOP-BOARD-6）；sop-verify 的看板客户端（SOP-VERIFY-2）。

技术选择沿用 SOP-BOARD-1：uv 管理的 Python 3.10 以上项目，标准库 HTTP 与 `sqlite3`，运行依赖只有 PyYAML，页面服务端渲染加原生 JavaScript，不加载外网资源。看板访问 Jira 用标准库 `urllib`。已有数据目录升级后继续可用：启动时补建新表，已有数据保持原样。

- 需求级验收：A1、A2、A3、A4、A5、A6、A7

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | 批次页面 | SOP 场景页就地复核；用例页上传与浏览；进度页；导出报告 | 测试岗、开发岗 | 看板存储 | sop-board | A1、A3、A5、A6 |
| self | /api/v1 写入接口 | 登记人工标记；上传用例；导入测试岗结果 | 测试岗页面、测试岗工具 | 看板存储 | sop-board | A2、A3、A6 |
| self | /api/v1 读取接口 | 读取用例、场景文件、报告、测试岗结果、Jira 单据摘要、批次状态 | 看板 | 测试岗、开发岗、sop-verify | sop-board | A3、A4、A5、A6 |

## SOP 场景页与就地复核
<!-- tw-section id=I1 type=interface depends=scope-global -->
批次页面共用一行导航：总览 `/p/<项目键>/b/<批次>`、SOP 场景 `…/scenarios`、用例 `…/cases`、进度 `…/progress`，当前页高亮。

**SOP 场景页** `/p/<项目键>/b/<批次>/scenarios` 按节点分组列出当前版本的全部场景。节点显示编号、标题、角色与入口描述；每条场景显示编号、标题、极性、优先级、角色、验证入口、初始状态、步骤、预期、禁止产生的影响、`refs`，以及：最新 Agent 结果（状态、执行者、本地时间）、最新人工结果（状态、标记人、本地时间、缺陷链接与 Jira 摘要）、所在队列、回归声明。场景编号链接到场景详情页。批次尚无场景版本时，页面写"尚无场景，测试岗上传用例后由合并生成"，并链接到用例页。

页面顶部有筛选：全部、未人工复核、Agent 已通过、不通过、受阻，各带数量。口径均针对场景的当前内容指纹：未人工复核为没有人工结果；Agent 已通过为最新 Agent 结果是 `pass`；不通过为最新结果是 `fail`；受阻为最新结果是 `blocked`。筛选在浏览器端完成，地址带 `?filter=<all|unreviewed|agent-pass|fail|blocked>` 以便分享，缺省与无法识别的取值都按全部显示。场景上显示的最新 Agent 结果与最新人工结果同样只取当前内容指纹的结果；内容变化后旧结果只在场景详情页的历史中出现。人工结果在页面上写"通过"、"不通过（失败）"或"不通过（受阻）"（总体方案 D7 的统一用语）。

**就地复核**。每条场景下方有"通过""失败""受阻"三个按钮：
- 点"通过"立即提交，不需填写其他内容。
- 点"失败"或"受阻"在该场景下展开"缺陷链接"（必填）与"备注"（选填）两栏和"提交""取消"按钮；缺陷链接为空或只有空白时不提交，在原处提示"请填写缺陷链接"。
- 提交后页面不跳转：该场景的最新人工结果、所在队列与筛选数量在原处更新；接口拒绝时在该场景下显示错误代码与各项问题的说明，已填内容保留。

标记人与写入令牌在页面顶部的身份栏填写一次，保存在浏览器 `localStorage`（键 `sop-board.marker`、`sop-board.token`），可随时更换；两项中任一项未填写时，点复核按钮先提示"请先在身份栏填写标记人与写入令牌"，不发送请求。场景详情页 `…/s/<场景编号>` 用同一组复核控件替换 SOP-BOARD-1 的复核表单，已从场景文件移除的场景不显示复核控件。详情页的每份证据都提供下载链接，文本预览只显示前 200 行时注明"只显示前 200 行"。证据下载接口对文本类证据（`text/*`、`application/json`、`application/x-ndjson`、`.md`、`.log`）的内容类型带 `charset=utf-8`。

## 人工标记接口
<!-- tw-section id=I2 type=interface depends=scope-global,D1 -->
`POST /api/v1/projects/<项目键>/batches/<批次>/marks`，凭写入令牌，正文 `{scenario, fingerprint, status, marker, defect_url, note}`。接口按这组字段改写，其余字段（如 `actual`、`environment`、`finding`、`evidence`）返回 422 `mark-invalid` 并点名字段：

- `status` 取 `pass`、`fail`、`blocked`；`marker` 为去掉首尾空白后非空的字符串；`note` 可省略，给出时为字符串。这些字段不合格时返回 422 `mark-invalid` 并点名字段。
- `fail` 与 `blocked` 必填 `defect_url`：缺少、不是字符串或去掉首尾空白后为空时返回 422 `mark-invalid` 并点名 `defect_url`；给出时按 I4 规范化与核对，不合规返回 422 `defect-link-invalid`。`pass` 带 `defect_url` 返回 422 `mark-invalid`。
- 场景与指纹的核对同 SOP-BOARD-1：场景不在当前版本或指纹不是当前内容返回 409 `scenario-stale`，`problems` 的 `field` 写 `scenario` 或 `fingerprint`。
- 看板生成实际结果文字：`pass` 为"人工复核通过"，`fail` 为"人工复核不通过（失败），缺陷 <规范化链接>"，`blocked` 为"人工复核不通过（受阻），缺陷 <规范化链接>"；`finding` 为规范化链接；另存观察记录 `observation-<轮次>.json` 作为证据，字段为 `marker`、`status`、`defect_url`、`note`、`recorded_at`（服务端时间，ISO 8601）；`defect_url` 与 `note` 没有值时省略。
- 登记为一个人工轮次，含一条结果，执行者为标记人，`executed_at` 为服务端时间，独立性 `undeclared`。返回 `{ok, round, scenario}`，`scenario` 为该场景在 SOP 场景页所需的最新摘要（状态、队列、最新人工结果），另含 `filters`（各筛选的数量）。

按轮导出的 `results.jsonl` 照旧只含 tw 结果契约字段；人工结果的 `actual`、`finding`、证据即上面生成的内容，可被 `tw team scenarios submit` 接受。

## 用例上传与浏览
<!-- tw-section id=I3 type=interface depends=scope-global,D1 -->
**上传** `POST /api/v1/projects/<项目键>/batches/<批次>/cases`，凭写入令牌，正文 `{filename, content, uploaded_by}`，`content` 为 YAML 原文。核对测试岗 Schema：

- 顶层只有 `metadata` 与 `test_cases`。`metadata` 必填 `module`、`feature`、`owner`（非空字符串）与 `last_reviewed`（`YYYY-MM-DD`），可选 `tags`（字符串数组），没有其他字段。
- `test_cases` 非空。每项必填 `id`（匹配 `^TC_[A-Z0-9_]{3,}_[0-9]{3}$`）、`title`、`priority`（`P0`、`P1`、`P2`）、`type`（`functional`、`ui`、`security`、`performance`、`accessibility`）、`steps` 与 `expected_results`（非空字符串数组，元素非空）、`tags`（非空字符串数组）；可选 `description`（字符串）、`preconditions`（字符串数组）、`auto`（布尔）；没有其他字段；同一文件内编号唯一。
- 不合格返回 422 `cases-invalid`，`problems` 每项 `{field, line, problem}`，`line` 为 YAML 中对应位置的行号；内容为空、顶层不是映射、缺少 `metadata` 或 `test_cases` 等文件级问题的 `line` 为 null；YAML 语法错误的 `problem` 写出解析器给出的原因与列号，推送场景文件时的语法错误同样如此。失败的上传不留下任何数据。

项目必须已存在，否则 404 `project-not-found`。批次不存在时随上传建立，状态为"尚无场景"，没有场景版本，可上传、浏览、下载用例。同一批次中同一编号以最后一次上传的完整内容为准，历次上传的并集是该批次的用例集。成功返回 `{ok, upload, added, updated, unchanged, union_count}`，`upload` 为批次内从 1 递增的上传序号；与上一次上传原文完全相同时返回 `unchanged_upload: true`，不产生新的上传序号。

**用例页** `/p/<项目键>/b/<批次>/cases`：上传控件（选择文件，上传人取身份栏的标记人），失败时逐条显示问题与行号；历次上传列表（序号、文件名、上传人、本地时间、条数、新增与更新数）；上传前身份栏未填标记人时提示填写；当前并集按用例 `tags` 中的 `section:` 分组显示（没有 `section:` 的归入"未分组"）（编号、标题、优先级、类型、前置条件、步骤、预期），每条标出当前场景版本中是否有同编号的场景（"已有对应场景""尚无对应场景"），有则链接到该场景。

**读取与下载**：`GET …/cases` 返回 `{uploads, cases, coverage}`，`coverage` 为 `{numerator, denominator, rate}`（有同编号场景的用例数 / 并集条数；分母为零时 `rate` 为 null）；`GET …/cases.yaml` 下载并集，`metadata` 取最后一次上传的值，符合测试岗 Schema；`GET …/scenario-file` 返回当前场景版本原文（`text/yaml`），批次尚无场景时 404 `scenario-file-not-found`。

## 缺陷链接与 Jira
<!-- tw-section id=I4 type=interface depends=scope-global -->
环境变量 `JIRA_URL`（形如 `https://<站点>.atlassian.net`，去掉末尾斜杠）、`JIRA_EMAIL`、`JIRA_API_TOKEN` 均可省略。缺陷链接去掉首尾空白后按顺序核对：

1. 配置了 `JIRA_URL` 且输入匹配 `^[A-Z][A-Z0-9_]*-[0-9]+$`：规范化为 `<JIRA_URL>/browse/<单号>`。
2. 配置了 `JIRA_URL` 且输入为 `<JIRA_URL>/browse/<单号>`（可带查询串或片段）：规范化为不带查询串与片段的形式。
3. 项目设置了缺陷链接前缀且输入以之开头（比较不区分大小写）：原样接受（SOP-BOARD-1 行为，含 GitHub 项目的缺省前缀）。
4. 其余返回 422 `defect-link-invalid`，说明接受的写法；未配置 `JIRA_URL` 而输入为单号时，说明需填写完整链接；项目没有缺陷链接前缀且未配置 `JIRA_URL` 时，说明需先设置项目的缺陷链接前缀或配置 `JIRA_URL`。

已登记的链接保持原样。

**Jira 摘要**：三个变量都配置时，`GET /api/v1/jira/<单号>` 由看板以邮箱与令牌（HTTP Basic）请求 `<JIRA_URL>/rest/api/3/issue/<单号>?fields=summary,status`，返回 `{ok, key, summary, status, url}`；单号不匹配上面的格式返回 422 `jira-key-invalid`，未配置返回 404 `jira-not-configured`，Jira 返回错误或超时（10 秒）时返回 502 `jira-unavailable` 并带原因。成功结果缓存 5 分钟，失败结果不缓存。页面上每个 Jira 链接旁由浏览器调用该接口显示"标题 · 状态"，失败时显示"Jira 读取失败"，不影响页面其余内容。令牌只在看板进程中使用，不出现在页面、接口响应与日志中。

## 状态、进度、刷新与报告
<!-- tw-section id=I5 type=interface depends=scope-global,D1 -->
**Agent 执行状态**。队列口径沿用 SOP-BOARD-1 的 D2：最新结果为 `fail` 或 `blocked` 的场景在问题队列，不在待执行队列。首页每个批次与批次总览顶部显示：批次尚无场景版本时为"尚无场景"；待执行队列非空时为"有待执行 N 条"；待执行队列为空时为"Agent 已测完"（批次有场景时，没有任何结果的场景都在待执行队列，因此此时至少有一条 Agent 结果），附最近一个 Agent 轮次的提交时间，以及当前场景最新 Agent 结果中通过、失败、受阻、跳过的数量。

**进度页** `/p/<项目键>/b/<批次>/progress`：开发侧与测试侧两列并排。开发侧：待执行数、已执行进度、Agent 验收通过率、一次通过率；测试侧：复测队列数（待复核）、已复核进度、人工复测通过率、误判数；下方为用例覆盖（有同编号场景的用例数 / 用例并集数）。比率写"分子/分母"，分母为零写"无数据"，口径同 SOP-BOARD-1 的 D2。

**轮询刷新**。`GET /api/v1/projects/<项目键>/batches/<批次>/state` 返回 `{ok, etag}`，响应头带 `ETag`；请求带 `If-None-Match` 且未变化时返回 304；项目或批次不存在时返回 404 `project-not-found` 或 `batch-not-found`。`etag` 由批次的最新场景版本号、最新轮次号与最新上传序号计算。批次的各页面每 3 秒轮询一次；变化时取回当前页面并替换主体内容。某条场景展开了失败或受阻的填写栏且已输入内容时（填写栏为空时照常替换），先在页面顶部提示"有新数据，提交或取消后刷新"，提交或取消后再替换。

**Markdown 报告** `GET /api/v1/projects/<项目键>/batches/<批次>/report.md`，总览页有"导出报告"链接。区块依次为：
1. 头部：项目、批次、场景版本与推送提交号、用例并集的最新上传序号、批次产品指纹、生成时间；尚无的项写"无"。项目或批次不存在时报告接口返回 404。
2. 用例覆盖：每条用例的编号、标题、对应场景（同编号）以及该场景最新 Agent 与人工结果；尚无对应场景的单列。
3. Agent 统计：Agent 验收通过率、一次通过率、每轮通过率表（含人工轮次，种类一列注明）。
4. 人工复测：人工复测通过率、复测覆盖率、误判明细（场景、缺陷链接、Agent 执行者、标记人）。
5. 修复记录、回归覆盖、合并记录：本期写"暂无记录"，由后续需求填充。
6. 未验证范围：最新结果为受阻或跳过的场景及其缺陷链接或原因。

比率写法同页面。报告中的数值与页面、接口使用同一套计算。

## 测试岗结果文件
<!-- tw-section id=I6 type=interface depends=scope-global,D1,I4 -->
**导出** `GET /api/v1/projects/<项目键>/batches/<批次>/results.yaml`，符合测试岗结果 Schema：顶层 `run_id`（`<批次>-<导出时间 YYYYMMDDHHMMSS>`）、`round`（批次最新轮次号）、`environment`（"sop-board 导出；产品提交 <提交号>"）、`results`。并集中每条有同编号场景且该场景当前指纹有结果的用例输出一项：`case_id`、`status`（`pass`→`passed`、`fail`→`failed`、`blocked`→`blocked`、`skip`→`skipped`）、`actual_result`、`executor`、`tested_at`（ISO 8601），以及存在时的 `jira_issue`（缺陷链接为 Jira 链接时取单号）与 `notes`（结果的备注；缺陷链接不是 Jira 链接时写"缺陷链接 <链接>"，与备注之间以"；"分隔）。批次尚无轮次时 `round` 为 0，尚无场景版本时 `environment` 写"sop-board 导出；产品提交 无"。取值为该场景当前指纹的最新结果。

**导入** `POST /api/v1/projects/<项目键>/batches/<批次>/results`，凭写入令牌，正文 `{content, uploaded_by}`，`content` 为测试岗工具产出的 `results.yaml` 原文。核对顶层必填 `run_id`、`round`、`environment`、`results`，每项必填 `case_id`、`status`、`actual_result`、`executor`、`tested_at`，可选 `duration_ms`、`evidence`、`jira_issue`、`notes`；`case_id` 必须对应当前版本中的同编号场景；`failed` 与 `blocked` 必须有 `jira_issue`，按 I4 规范化；`tested_at` 不带时区时按看板所在机器的本地时区解释；`skipped` 的项不登记，在响应中列出。任何一项不合格返回 422 `results-invalid` 并逐项列出，不留下任何数据。合格时登记为一个人工轮次，每项一条结果：执行者取 `executor`，执行时间取 `tested_at`，实际结果取 `actual_result`，`finding` 为规范化链接，`note` 取 `notes`，证据为看板生成的导入记录 `import-<轮次>.json`（上传人、`run_id`、`round`、`environment`、导入时用例并集的最新上传序号与该项原文）。返回 `{ok, round, imported, skipped}`；全部为 `skipped` 时不建立轮次，`round` 为 null，`imported` 为 0。

## 存储
<!-- tw-section id=D1 type=data depends=scope-global -->
在 SOP-BOARD-1 的存储上增加两张表，启动时以"不存在则建立"补建，已有数据保持原样：

- 用例上传：批次、上传序号、文件名、上传人、上传时间、原文 sha256、`metadata` 原文 JSON、条数、新增数、更新数；批次内上传序号唯一。
- 用例版本：批次、用例编号、上传序号、该条用例原文 JSON；批次内同一编号可有多个版本，并集取上传序号最大的版本。

只凭用例建立的批次在批次表中场景文件路径与产品指纹为空字符串，没有场景版本。Jira 摘要缓存只在内存中。人工标记与导入沿用 SOP-BOARD-1 的轮次、结果与证据表。

## 验收 A1：SOP 场景页与就地复核
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1,I2 -->
SOP-21 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-21
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D6、D7、D13、S3；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I1、I2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A2：标记接口与导出
<!-- tw-section id=A2 type=acceptance depends=scope-global,I2,D1 -->
SOP-22 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-22
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D6、D7、D8；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I2、I4 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A3：用例上传与浏览
<!-- tw-section id=A3 type=acceptance depends=scope-global,I3,D1 -->
SOP-23 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-23
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D13、D18；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I3、D1 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A4：缺陷链接与 Jira
<!-- tw-section id=A4 type=acceptance depends=scope-global,I4 -->
SOP-24 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-24
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D7；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I4 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A5：状态、进度与刷新
<!-- tw-section id=A5 type=acceptance depends=scope-global,I5 -->
SOP-25 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-25
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D13、D26、S3；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I5 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A6：报告与测试岗结果
<!-- tw-section id=A6 type=acceptance depends=scope-global,I5,I6 -->
SOP-26 节点全部场景通过。

- 场景：docs/sop/sop-board-2.yaml#SOP-26
- 依据：位置 docs/proposals/2026-10-19-standalone-sop-verification.md 的 D18、D27；版本 v8（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的 I5、I6 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A7：SOP-BOARD-1 中受影响的场景
<!-- tw-section id=A7 type=acceptance depends=scope-global,I2,I4 -->
`docs/sop/sop-board.yaml` 的 SOP-01、SOP-05、SOP-06、SOP-07 节点全部场景通过。删除 SOP-04 后，SOP-05-P01、SOP-06-P01、SOP-06-P03 改写初始状态，SOP-07-P01、SOP-07-N01 按 I2、I4 改写缺陷链接的预期并覆盖项目没有前缀的情形；SOP-06-P09 的人工受阻改填缺陷链接；SOP-01-N02 改写普通文件作数据目录时的预期；其余场景由已声明的回归守护。

- 场景：docs/sop/sop-board.yaml#SOP-01、SOP-05、SOP-06、SOP-07
- 依据：位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 3；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-2.md 的全局范围与 I2、I4 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 任务 T1：SOP 场景页与就地复核
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2,I4,D1 -->
实现批次导航、SOP 场景页、筛选、就地复核控件（场景页与详情页共用）、身份栏，并按 I2 改写标记接口与导出；数据目录为普通文件时的错误说明按全局范围修改。缺陷链接的核对先实现 I4 的规范化与前缀规则（Jira 摘要在 T3）。测试在临时数据目录与空闲端口以子进程启动真实服务，经 HTTP 核对响应、SQLite 行、页面文本与导出文件；页面交互用 Node.js 运行 `board.js` 的隔离测试或等价方式核对；先写失败测试再实现。

- 验收：A1、A2、A7
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T2：用例上传与浏览
<!-- tw-section id=T2 type=task depends=scope-global,I3,D1 -->
在 T1 之后实施。实现用例上传与 Schema 核对（含行号）、只凭用例建立批次、并集、用例页、用例与场景文件下载。先写失败测试再实现。

- 验收：A3
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T3：Jira 摘要
<!-- tw-section id=T3 type=task depends=scope-global,I4 -->
在 T2 之后实施。实现 Jira 摘要接口、缓存与页面显示。测试以本机启动的模拟 Jira 服务核对请求的认证头、地址与字段，以及失败与超时时的页面表现；令牌不出现在响应与页面中。先写失败测试再实现。

- 验收：A4
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T4：状态、进度、刷新、报告与测试岗结果
<!-- tw-section id=T4 type=task depends=scope-global,I5,I6,D1 -->
在 T3 之后实施。实现 Agent 执行状态、进度页、状态接口与轮询刷新、Markdown 报告、测试岗结果文件的导出与导入。报告与导出的数值在测试中用独立计算的预期值核对。先写失败测试再实现。

- 验收：A5、A6
- 范围：src、tests、README.md
- 前置任务：无
