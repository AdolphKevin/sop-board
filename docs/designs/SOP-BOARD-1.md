---
design: "D-sop-board-1"
task: "SOP-BOARD-1"
revision: 2
assessment: {"complexity":"complex","risks":["persistence","public-contract"],"effort":"约两个工作日代理时长","drivers":["新建服务：启动、存储、写入与读取接口、页面","接口与导出格式被 sop-verify 与 team-workflow 使用","测试岗与开发岗在同一份数据上协作，人工标记不得改写 Agent 结果"],"uncertainties":[],"required_skills":["Python","SQLite","HTTP","HTML 与原生 JavaScript"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-1：看板核心

> 本文件是 tw 任务 SOP-BOARD-1 的方案原文。总体设计与决定见 team-workflow 仓库的 `docs/proposals/2026-10-19-standalone-sop-verification.md`（v7），本需求对应其第 8 节第二行。验收场景见 [docs/sop/sop-board.yaml](../sop/sop-board.yaml)。
>
> 状态：修订 2。修订 1 由 AdolphKevin 于 2026-10-19 批准；修订 2 按第 1 轮验收的三条观察补充启动与时间显示要求，AdolphKevin 同日要求本期修复。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
建立看板服务的核心：成员 clone 后用 uv 在本机启动；开发岗代理推送场景文件、证据与 Agent 轮次；测试岗在页面上复核 Agent 判为通过的场景并标记；页面展示三个队列、Agent 验收通过率、人工复测通过率与测试进度；按轮次导出 team-workflow 能登记的结果。

非目标：测试用例上传与配对、反馈线程、轮询刷新、一键创建 GitHub Issue、测试岗结果 Schema 的导入导出（SOP-BOARD-2）；SOP 原文与版本页、Codex 分析任务（SOP-BOARD-3）；sop-verify 的推送客户端（SOP-VERIFY-2）；服务器部署与登录。

技术选择：uv 管理的 Python 3.10 以上项目，命令入口 `sop-board`；Web 层用标准库 `http.server` 的多线程服务器；存储用标准库 `sqlite3`；运行依赖只有 PyYAML，开发依赖 pytest。页面由服务端渲染 HTML，交互用原生 JavaScript，不加载外网资源。

- 需求级验收：A1、A2、A3、A4、A5、A6

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | uv run sop-board | 启动、停止、内网访问 | 启动看板的成员 | 团队成员 | sop-board | A1 |
| self | /api/v1 写入接口 | 推送场景文件；上传证据；登记 Agent 轮次；登记人工标记 | sop-verify 与开发岗代理、测试岗页面 | 看板存储 | sop-board | A2、A3、A4、A5 |
| self | 页面与 /api/v1 读取接口 | 查看队列、统计与场景历史；导出轮次 | 看板 | 测试岗、开发岗、team-workflow | sop-board | A4、A5、A6 |

## 启动与页面
<!-- tw-section id=I1 type=interface depends=scope-global -->
启动：`uv run sop-board --data <目录> [--host 127.0.0.1] [--port 8787]`。环境变量 `SOP_BOARD_TOKEN` 必须非空。先核对令牌并占用端口，成功后再准备数据目录；数据目录不存在时创建。成功时在标准输出写一行 `看板地址：http://<host>:<port>/`；`--host 0.0.0.0` 时这一行写 `127.0.0.1`，并逐行写出本机每个内网 IPv4 地址对应的 `内网地址：http://<地址>:<port>/`。以下情况以退出码 1 结束并在错误输出说明：缺少 `SOP_BOARD_TOKEN`；端口已被占用（含端口号）；数据目录无法创建或不可写（含路径）。启动失败时数据目录中不留下新文件；数据目录原本不存在时，失败后仍不存在。收到中断或终止信号时关闭监听与数据库后退出。

项目键：项目身份转小写，每段 `[a-z0-9]` 以外的连续字符换成一个 `-`，去掉首尾的 `-`。例如 `github.com/demo-org/demo-app` 的键是 `github-com-demo-org-demo-app`。

页面：

| 地址 | 内容 |
| --- | --- |
| `/` | 项目与批次列表 |
| `/p/<项目键>/b/<批次>` | 批次总览：数据 D2 的各项指标、三个队列的场景列表、进度、轮次表、误判明细 |
| `/p/<项目键>/b/<批次>/s/<场景编号>` | 场景详情：场景内容与入口、按内容指纹分组的结果历史（旧指纹标"场景已变更"，已移除的场景标"已从场景文件移除"）、证据预览（图片直接显示，文本显示前 200 行，其他提供下载）、标记表单 |

标记表单首次使用时请填写标记人与令牌，二者保存在浏览器 `localStorage`，可在页面上更换。表单提交到写入接口的人工标记；状态为 `fail` 时必须填写缺陷链接，接口拒绝时页面显示原因并保留已填内容。比率写成"分子/分母"，分母为零时写"无数据"。页面上的所有时间统一换算到看板所在机器的本地时区，写成 `YYYY-MM-DD HH:MM:SS ±HH:MM`，不带微秒；接口与导出保留原始的 ISO 8601 值。

## 写入接口
<!-- tw-section id=I2 type=interface depends=scope-global,D1 -->
写入请求带 `Authorization: Bearer <SOP_BOARD_TOKEN>`，缺失或不符返回 401，`error_code` 为 `unauthorized`。请求与响应为 UTF-8 JSON（证据上传除外）。失败响应为 `{ok: false, error_code, problems}`，`problems` 每项 `{field, problem}`，场景文件问题另含 `line`。失败的请求不留下任何数据。

**推送场景文件** `PUT /api/v1/projects/<项目键>/batches/<批次>/scenario-file`，正文 `{project: {id, name}, path, commit, dirty, product_digest, content, fingerprints, pushed_by}`：

- `content` 是场景文件原文，看板用 PyYAML 安全读取，按 sop-verify 的场景文件规则核对（入口名只要求为非空字符串），不合格返回 422 `scenario-file-invalid`；`metadata.batch` 与地址中的批次不同、或 `project.id` 的键与地址不同，返回 422 `project-mismatch`。
- 看板按数据 D1 的规则计算每条场景的内容指纹，与 `fingerprints` 对照，任何一条不同返回 409 `fingerprint-mismatch` 并列出编号。
- 成功时建立项目与批次，保存一个场景版本，更新批次的最新产品指纹，返回 `{ok, version, added, changed, removed, unchanged}`。内容与当前版本完全相同时返回 200、`unchanged: true`，不产生新版本。

**上传证据** `POST /api/v1/evidence`，正文为文件原始字节，`X-Evidence-Name` 头为百分号编码的文件名。单个文件超过 64 MiB 返回 413 `evidence-too-large`。按 sha256 保存，相同内容只存一份；返回 `{ok, sha256, size}`。

**登记 Agent 轮次** `POST /api/v1/projects/<项目键>/batches/<批次>/rounds`，正文 `{idempotency_key, scenario_file_sha256, product: {commit, digest, dirty}, implementer, submitted_by, results}`，`implementer` 可省略；`results` 每项 `{scenario, fingerprint, status, actual, evidence, executed_at, executor, session, finding, note}`，`evidence` 为 `[{name, sha256}]`，后三项可省略：

- `scenario_file_sha256` 不是当前版本返回 409 `scenario-file-stale`；某条结果的场景不在当前版本或 `fingerprint` 不是当前内容返回 409 `scenario-stale` 并点名。
- 逐条核对：`status` 取 `pass`、`fail`、`blocked`、`skip`；`pass` 与 `fail` 至少一份证据；证据的 sha256 必须已上传，否则 `evidence-missing`；`blocked` 与 `skip` 必填 `finding`；`executed_at` 带时区且不晚于服务端当前时间五分钟；同一轮每个场景一条；`results` 非空。问题返回 422 `round-invalid`，`field` 写作 `results[<序号>].<字段>`。
- 独立性：给出 `implementer` 时，执行者与实现者的姓名折叠大小写、去掉首尾空白并把连续空白合为一个后相同为 `self-check`，不同为 `independent`；未给出为 `undeclared`。
- 同一批次内 `idempotency_key` 已用过且正文相同，返回已有的轮次；正文不同返回 409 `idempotency-conflict`。
- 成功返回 `{ok, round, statistics, queues}`。

**登记人工标记** `POST /api/v1/projects/<项目键>/batches/<批次>/marks`，正文 `{scenario, fingerprint, status, actual, marker, environment, evidence, defect_url, finding, note}`，后四项可省略：

- 场景与指纹的核对同上。`marker` 与 `actual` 非空。
- `fail` 必填 `defect_url`，且以项目的缺陷链接前缀开头，否则 422 `defect-link-required`；项目没有前缀时 422 `defect-prefix-unset`。`blocked` 与 `skip` 必填 `finding`。
- 没有附证据时，看板生成一份观察记录 `observation-<轮次>.json`（标记人、环境、状态、实际结果、时间）作为证据保存。
- 登记为一个人工轮次，含一条结果：执行者为标记人，`executed_at` 为服务端时间，`fail` 的 `finding` 为缺陷链接，独立性为 `undeclared`。返回 `{ok, round, queues}`。

**项目设置** `PUT /api/v1/projects/<项目键>`，正文 `{defect_prefix}`，前缀以 `https://` 开头。身份为 `github.com/<所有者>/<仓库>` 的项目，缺省前缀为 `https://github.com/<所有者>/<仓库>/issues/`。

## 读取与导出接口
<!-- tw-section id=I3 type=interface depends=scope-global,D1,D2 -->
读取接口与页面不需要令牌。

| 地址 | 返回 |
| --- | --- |
| `GET /api/v1/projects` | 项目列表，每项含键、身份、名称与批次摘要 |
| `GET /api/v1/projects/<项目键>/batches/<批次>` | 当前版本号、数据 D2 的指标、三个队列的数量、进度、轮次表与误判明细 |
| `GET /api/v1/projects/<项目键>/batches/<批次>/scenarios` | 当前版本的每条场景：编号、节点、标题、极性、优先级、入口、指纹、所在队列、最新 Agent 结果、最新人工结果、回归声明 |
| `GET /api/v1/projects/<项目键>/batches/<批次>/rounds/<轮次>` | 该轮的种类、提交信息与逐条结果 |
| `GET /api/v1/projects/<项目键>/batches/<批次>/rounds/<轮次>/export` | ZIP：`results.jsonl` 与 `evidence/<序号>-<文件名>` |
| `GET /api/v1/evidence/<sha256>` | 证据原始字节，按文件名给出内容类型 |

导出的 `results.jsonl` 每行只含 team-workflow 结果契约的字段：`scenario`、`status`、`actual`、`evidence`（相对 ZIP 根目录的路径）、`executed_at`、`executor`，以及存在时的 `session`、`finding`、`note`。不存在的项目、批次、轮次或证据返回 404，`error_code` 分别为 `project-not-found`、`batch-not-found`、`round-not-found`、`evidence-not-found`。页面与这些接口使用同一套计算。

## 存储
<!-- tw-section id=D1 type=data depends=scope-global -->
数据目录中 `board.sqlite3` 使用 WAL；证据存为 `evidence/<sha256 前两位>/<sha256>`。逻辑表：项目（键、身份、名称、缺陷前缀）；批次（项目、批次名、场景文件路径、最新产品指纹）；场景版本（批次、版本号、原文、sha256、路径、提交号、`dirty`、推送人、时间）；场景（批次、编号、内容指纹、节点、标题、极性、优先级、入口、回归声明、正文、是否在当前版本）；证据（sha256、大小、首次文件名）；轮次（批次、轮次号、种类 `agent` 或 `manual`、场景版本、产品提交号与指纹与 `dirty`、实现者、提交人、提交时间、幂等键与正文摘要）；结果（轮次、场景、内容指纹、状态、实际结果、执行时间、执行者、会话、`finding`、`note`、独立性）；结果证据（结果、序号、文件名、sha256）。

轮次号在批次内从 1 递增，Agent 轮次与人工轮次共用；先后按提交顺序判定，不按 `executed_at`。用同一数据目录重启后数据完整保留。

内容指纹：取场景的 `initial_state`、`steps`、`expected`、`forbidden_effects` 组成对象，字符串中的 `\r\n` 与 `\r` 换成 `\n`，按 `json.dumps(对象, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` 序列化，取 UTF-8 字节的 sha256 十六进制。与 sop-verify 方案 D1 的定义相同。

## 队列与统计
<!-- tw-section id=D2 type=data depends=scope-global,D1 -->
只统计当前版本中的场景，每条场景只看其当前内容指纹的结果。记 A 为最新 Agent 结果，M 为最新人工结果，L 为最新结果（不分种类）。

| 队列或状态 | 条件 |
| --- | --- |
| 待执行 | 没有任何结果；或 A 为 `pass` 且 L 就是 A，A 所在轮次的产品指纹与批次最新产品指纹不同，且未声明 `regression.check` |
| 问题 | L 的状态为 `fail`、`blocked` 或 `skip` |
| 复测 | A 为 `pass` 且 L 就是 A，且不在待执行 |
| 已复核通过 | L 是人工的 `pass` |

| 指标 | 分子 / 分母 |
| --- | --- |
| Agent 验收通过率 | A 为 `pass` 的场景数 / A 为 `pass` 或 `fail` 的场景数 |
| 人工复测通过率 | A 为 `pass` 且其后有人工结果的场景中，最新人工结果为 `pass` 的数 / 最新人工结果为 `pass` 或 `fail` 的数 |
| 复测覆盖率 | A 为 `pass` 且其后有人工结果的场景数 / A 为 `pass` 的场景数 |
| 一次通过率 | 每条场景当前指纹的首个 Agent 结果中 `pass` 的数 / `pass` 与 `fail` 之和 |
| 已执行进度 | 有任何结果的场景数 / 当前场景总数 |
| 已复核进度 | 同复测覆盖率 |
| 每轮通过率 | 该轮 `pass` 数 / 该轮结果数 |

误判明细列出当前场景的每一次误判：人工 `fail` 结果，其之前最近的 Agent 结果为 `pass`；每项含场景、缺陷链接、Agent 执行者与标记人。比率在接口中为 `{numerator, denominator, rate}`，分母为零时 `rate` 为 null。

## 验收 A1：启动与持久化
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1,D1 -->
SOP-01 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-01
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A2：推送场景文件
<!-- tw-section id=A2 type=acceptance depends=scope-global,I2,D1 -->
SOP-02 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-02
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A3：证据与 Agent 轮次
<!-- tw-section id=A3 type=acceptance depends=scope-global,I2,D1,D2 -->
SOP-03 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-03
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A4：人工复核
<!-- tw-section id=A4 type=acceptance depends=scope-global,I1,I2,D2 -->
SOP-04 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-04
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A5：场景变更
<!-- tw-section id=A5 type=acceptance depends=scope-global,I1,I2,D2 -->
SOP-05 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-05
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 验收 A6：总览与导出
<!-- tw-section id=A6 type=acceptance depends=scope-global,I1,I3,D2 -->
SOP-06 节点全部场景通过。

- 场景：docs/sop/sop-board.yaml#SOP-06
- 依据：位置 AdolphKevin/team-workflow docs/proposals/2026-10-19-standalone-sop-verification.md 第 3、4 节；版本 v7（2026-10-19）；确认人 AdolphKevin / 位置 docs/designs/SOP-BOARD-1.md 的 I1、I2、I3、D1、D2 节；版本 修订 2；确认人 AdolphKevin
- 检查：unit
- 验证：自动

## 任务 T1：骨架、存储与接口
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2,I3,D1,D2 -->
建立 uv 项目（`pyproject.toml`、`uv.lock`、命令入口 `sop-board`、`src/sop_board/`），实现启动、存储、写入与读取接口和统计计算。测试放在 `tests/`，每个测试在临时数据目录与空闲端口以子进程启动真实服务，经 HTTP 核对响应、SQLite 行与证据文件；先写失败测试再实现。本任务一并提交本仓的 tw 接入文件。

- 验收：A1、A2、A3
- 范围：pyproject.toml、uv.lock、src、tests、.gitignore、README.md、.tw/config.json、AGENTS.md、CLAUDE.md、docs/verification.md
- 前置任务：无

## 任务 T2：页面、复核与导出
<!-- tw-section id=T2 type=task depends=scope-global,I1,I2,I3,D1,D2 -->
在 T1 交回并通过审核之后实施。实现三个页面、标记表单与浏览器端的标记人和令牌保存、场景变更与移除的显示、轮次导出 ZIP。测试核对页面文本中的数值与接口一致、标记表单的拒绝提示、导出文件的字段；先写失败测试再实现。README 补充启动与内网访问说明。

- 验收：A4、A5、A6
- 范围：src、tests、README.md
- 前置任务：无

## 任务 T3：启动与时间显示修正
<!-- tw-section id=T3 type=task depends=scope-global,I1 -->
在 T2 之后实施。按修订 2 的 I1：调整启动顺序，失败时不在数据目录留下新文件；`--host 0.0.0.0` 时写出可直接打开的内网地址；页面时间统一为本地时区、不带微秒。先写失败测试再实现。

- 验收：A1、A6
- 范围：src、tests、README.md
- 前置任务：无
