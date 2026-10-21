---
design: "D-sop-board-9"
task: "SOP-BOARD-9"
revision: 3
assessment: {"complexity":"medium","risks":["persistence","public-contract","authorization","cross-service"],"effort":"约半个到一个工作日代理时长，分两段","drivers":["自检在端口只剩 TIME_WAIT 连接时误报占用","缺陷链接按前缀校验，origin 不是 GitHub 又没配 JIRA_URL 时测试同事无法标不通过","Jira 只能靠 JIRA_URL 配置识别","修复在出现第一个失败后就开始，其他批次还没验完或测试同事还在复核，方案只看到部分失败项"],"uncertainties":[],"required_skills":["Python","SQLite","HTML 与原生 JavaScript","Jira REST API"]}
roles: {"design_owner":"Claude","implementation_owner":"Codex","acceptance_owner":"AdolphKevin"}
---

# SOP-BOARD-9：缺陷链接原样保存、按链接识别 Jira、验证结束后统一修复

> 本文件是 tw 任务 SOP-BOARD-9 的方案原文，修订 v9 方案 D34 的修复触发方式，并取代原有的缺陷链接前缀规则。验收只以场景经真实入口执行为准，开发中不写单元、接口与集成测试。
>
> 状态：修订 3。修订 3 新增 D44（搭建时引导配置 Jira 读取）与 D45（SOP-BOARD-8 独立评审 Q3–Q5：内网不显示本机路径、搭建指引与手册更正），2026-10-20 经 AdolphKevin 批准（会话答复“批准”与“按你建议进行”）。Jira 站点与凭据由各项目使用方自行配置，文中 channelwill 链接只是格式示例；SOP-91-P02、SOP-94-P01 由项目使用方用自己的凭据与测试单据执行（2026-10-20 用户说明）。修订 1 于 2026-10-20 经 AdolphKevin 批准；修订 2 并入 SOP-BOARD-8 独立验收 A 发现的自检端口误报（D43），2026-10-20 经 AdolphKevin 批准。修订 1 批准时批准前确认：缺陷链接不校验、不加前缀，填什么存什么；含 atlassian 的链接识别为 Jira，用 API token 读取单据；SOP 验证运行完后汇总全部失败项统一修复；人工失败在测试同事点“复核完成”后统一修复；不通过时缺陷链接必填，内容不校验。

## 全局范围
<!-- tw-section id=scope-global type=global-scope -->
起因：SOP-BOARD-8 独立验收中发现，仓库 origin 不是 GitHub、又没配 `JIRA_URL` 时，测试同事在页面标不通过会得到 422，页面和文档都没有设置前缀的入口（D1）；修复在 Agent 判失败后就开始，人工标记前修复已经提交（O1）。另外，看板每 5 秒检查一次，只要有失败项、没有进行中的修复就建任务，只等同一工作区正在运行的执行，不等排队中的执行。所以其他批次还没验完、测试同事还在复核时，修复就可能先带着部分失败项开工，方案只顾到局部。

本任务：缺陷链接原样保存；只按链接本身识别 Jira；Agent 失败在整个工作区的验证运行完后统一修复，人工失败在测试同事点“复核完成”后统一修复。

非目标：自托管 Jira（域名不是 `*.atlassian.net`）；其他缺陷平台的读取；修复会话内部的方案与提交流程；修复任务运行中扩大范围（新失败项等下一个修复任务）；数据格式迁移。

- 需求级验收：A1、A2、A3、A4

| 仓库 | 入口 | 操作 | 产生方 | 使用方 | 提交负责方 | 验收 |
| --- | --- | --- | --- | --- | --- | --- |
| self | SOP 场景页与 `POST …/marks`、结果文件导入 | 标不通过并填缺陷链接 | 测试同事 | 看板、修复会话 | sop-board | A1 |
| self | Jira Cloud REST API | 读取单据、合入后回写 | 看板 | 修复会话、测试同事 | sop-board | A1 |
| self | 批次页、SOP 场景页与 `POST …/review-complete` | 复核完成，触发统一修复 | 测试同事 | 看板 | sop-board | A2 |
| self | `sop-board check` 与本机页面“自检” | 第 2 步端口核对 | 开发者 | 看板 | sop-board | A3 |
| self | 搭建会话（`sop-board guide`）与环境栏 | 引导配置 Jira 读取；内网不显示本机路径 | 开发者与其代理、内网同事 | 看板 | sop-board | A4 |

## 缺陷链接原样保存（D40）
<!-- tw-section id=I1 type=interface depends=scope-global -->
`POST …/marks` 中 status 为 fail 时，`defect_url` 必须是非空文字，去掉首尾空白后不能为空，否则返回 422 `defect-link-required`；除此之外不校验、不改写、不补前缀，按提交的原文保存。结果文件导入（`results.yaml` 等）中的缺陷链接按同一规则处理。

删除工作区缺陷链接前缀：不再从 origin 推导前缀，`PUT /api/v1/workspaces/<名>` 的 `defect_prefix` 一并删除；`workspaces.defect_prefix` 列保留但不再读写。原来把 Jira 裸单号（如 `ABC-1`）拼成完整链接的做法也删除，裸单号按文字保存。

页面、报告和导出原样显示缺陷链接。只有 scheme 为 `http` 或 `https` 的链接渲染成可点击的 `<a>`（带 `rel="noopener noreferrer"`），其余内容一律作为转义后的纯文本显示。

## 按链接识别 Jira（D41）
<!-- tw-section id=I2 type=interface depends=scope-global,I1 -->
缺陷链接同时满足以下三条时识别为 Jira：scheme 为 `https`；主机名转小写后以 `.atlassian.net` 结尾；路径为 `/browse/<单号>`，单号匹配 `[A-Z][A-Z0-9_]*-[0-9]+`。查询参数和片段忽略，例如 `https://channelwill.atlassian.net/browse/CACB-657?atlOrigin=…` 识别为站点 `https://channelwill.atlassian.net` 上的单号 `CACB-657`。不满足的链接，包括路径或查询参数里含 `atlassian` 但主机名不符的，都不识别为 Jira，不发出任何携带凭据的请求。

Jira 站点取链接中的 `https://<主机名>`，不再读取 `JIRA_URL`。认证沿用 `JIRA_EMAIL`、`JIRA_API_TOKEN` 环境变量，只发往识别出的站点，不跟随重定向。三处使用：

- 页面摘要：识别为 Jira 的链接旁显示单据标题和状态（沿用现有缓存与超时）。
- 修复输入：修复会话准备阶段读取单据的标题、描述、状态、评论和附件名，与现有内容相同。
- 合入回写：修复合入后按单据所在站点评论，并按 `JIRA_FIXED_STATUS` 流转，规则同现状。

凭据缺失或读取失败时链接照常保存，页面写明“Jira 未配置”或读取失败的原因，修复输入写明未能读取；凭据值不进入页面、日志、报告或修复输入。README 和搭建指引删去 `JIRA_URL`。

## 验证结束后统一修复（D42，修订 D34 的触发方式）
<!-- tw-section id=I3 type=interface depends=scope-global -->
“验证运行完”指整个工作区没有状态为排队中、待提交、等待浏览器、运行中的执行任务。修复任务仍按工作区计，同一时间最多一个。

- Agent 失败：只在验证运行完、且没有未结束的修复任务时，看板才汇总全部批次中可修复的 Agent 失败建一个修复任务。验证还没运行完时，已经出现的 Agent 失败只积累，不建任务。
- 人工失败：测试同事标的不通过只积累，不建任务。测试同事在批次概览页或 SOP 场景页点“复核完成，开始修复（K 条人工不通过）”后，该批次中在此之前标记的人工失败变为可修复。K 是该批次尚未进入修复的人工不通过数，为 0 时按钮停用并写明“没有待修复的人工不通过”。之后的建任务条件与 Agent 失败相同：验证运行完、且没有未结束的修复任务时，与当时可修复的 Agent 失败一起建一个修复任务。
- “复核完成”是测试同事的操作：本机和内网页面都显示，接口 `POST …/batches/<批次>/review-complete` 的正文为 `{"marker":"<标记人>"}`。标记人为空返回 422；K 为 0 返回 409 `nothing-to-fix`。接口照常检查同源和 JSON 类型，返回本次纳入的场景列表，以及修复会在什么时候开始（立即、等待执行结束或等待当前修复结束）。登记写入新表 `review_completions(batch, marker, completed_at)`。表随启动时的建表语句自动创建，不升级数据格式。
- 全局暂停自动任务时，不建修复任务；恢复后按以上条件建。修复页与批次页显示等待原因：“等待验证运行完”“等待测试同事复核完成”“等待当前修复结束”。

## 端口核对只认实际监听（D43）
<!-- tw-section id=I4 type=interface depends=scope-global -->
自检第 2 步只把确有进程监听的声明端口判为占用。探测绑定时开启地址复用（`SO_REUSEADDR`，与服务端常见做法相同），上一轮环境或健康检查留下的 TIME_WAIT 等已关闭连接不算占用；绑定仍失败时用 `lsof -nP -iTCP:<端口> -sTCP:LISTEN` 列出监听进程，规则与现状相同。看板启动前的同一核对使用相同规则。

## 搭建时引导配置 Jira 读取（D44）
<!-- tw-section id=I5 type=interface depends=scope-global,I2 -->
搭建指引新增一步“配置 Jira 读取”。代理先问开发者：团队的缺陷链接是否为 Jira Cloud（`*.atlassian.net`）。

- 是：代理说明需要两个环境变量，并给出 API token 的创建地址（Atlassian 账号安全设置页）。`JIRA_EMAIL` 是 Atlassian 账号邮箱，`JIRA_API_TOKEN` 是 API token。由开发者自己设置在启动 `sop-board` 的环境里，例如自己的 shell 配置或密钥管理工具。
- 代理不请开发者把值贴进对话，不读取、不写入、不回显这两个值。只按变量名核对是否已设置，例如 `[ -n "$JIRA_API_TOKEN" ] && echo 已设置`。凭据值不写进数据目录、适配、技能或搭建报告。
- 看板启动后，代理请开发者提供一张真实单据的链接，通过本机的 `GET /api/v1/jira/summary?url=…` 读取。报告只写读到的标题与状态，或失败原因。
- 否：跳过这一步，缺陷链接照样原样保存。

本机页面的环境栏增加一行“Jira 读取：已配置 / 未配置”，只看两个变量是否非空，不显示值；内网页面不显示这一行。

## 内网不显示本机路径，指引与手册更正（D45）
<!-- tw-section id=I6 type=interface depends=scope-global -->
来自 SOP-BOARD-8 独立评审的 Q3–Q5：

- **Q3**：非本机请求的环境栏搭建说明（`setup_instructions`）只写“尚未搭建，请开发者在本机处理”，不带 `codex -C`、`--data` 等命令和任何本机绝对路径。`/api/v1/workspaces` 的 `execution.*_reason` 与 `/entries` 的问题文字，在非本机请求中也去掉本机绝对路径：用 `<数据目录>`、`<仓库:别名>` 这样的占位代替，或只保留问题类别。本机请求不变。
- **Q4**：`docs/verification.md` 的 SOP-83 一节改写：按钮的待执行条数用的是发现线程每 5 秒更新的批次产品摘要，最多滞后 5 秒；点击入队时才按当时内容精确计算。
- **Q5**：搭建指引第 1 步与 README 写明，`sop-board init` 也要写 `~/.sop-board/<名称>`，与 `uv tool install`、`sop-board check` 一样需要在沙箱外运行，由代理申请批准。

## 验收 A1：缺陷链接与 Jira
<!-- tw-section id=A1 type=acceptance depends=scope-global,I1,I2 -->
SOP-91 节点全部场景通过；受影响的 SOP-73-P01 按新规则重跑。

- 场景：docs/sop/sop-board-9.yaml#SOP-91
- 依据：位置 本文 D40、D41；版本 修订 1（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A2：统一修复
<!-- tw-section id=A2 type=acceptance depends=scope-global,I3 -->
SOP-92 节点全部场景通过；受影响的 SOP-74-P01 按新触发方式重跑。

- 场景：docs/sop/sop-board-9.yaml#SOP-92
- 依据：位置 本文 D42；版本 修订 1（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A3：端口核对
<!-- tw-section id=A3 type=acceptance depends=scope-global,I4 -->
SOP-93 节点全部场景通过；受影响的 SOP-71-N01 重跑。

- 场景：docs/sop/sop-board-9.yaml#SOP-93
- 依据：位置 本文 D43；版本 修订 2（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 验收 A4：Jira 搭建引导与内网信息
<!-- tw-section id=A4 type=acceptance depends=scope-global,I5,I6 -->
SOP-94、SOP-95 节点全部场景通过。

- 场景：docs/sop/sop-board-9.yaml#SOP-94、SOP-95
- 依据：位置 本文 D44、D45；版本 修订 3（2026-10-20）；确认人 AdolphKevin
- 检查：无

## 任务 T1：缺陷链接与 Jira
<!-- tw-section id=T1 type=task depends=scope-global,I1,I2 -->
第一段。起点：测试同事在 origin 为虚构地址、没有 `JIRA_URL` 的工作区标不通过；终点：任意文字的链接都能保存并原样显示，Atlassian 链接旁显示 Jira 单据标题，修复输入带上单据内容。实现 I1、I2，更新 README、`docs/verification.md`、搭建指引中涉及 `JIRA_URL` 与缺陷链接的部分。

- 验收：A1
- 范围：src、README.md、docs/verification.md、docs/designs、docs/sop
- 前置任务：无

## 任务 T2：验证结束后统一修复
<!-- tw-section id=T2 type=task depends=scope-global,I3 -->
第二段。起点：两个批次的执行先后排队，其中各有一条场景失败；终点：两个执行都结束后只建一个修复任务，包含两条失败；测试同事标的人工失败在点“复核完成”之后才进入下一个修复任务。实现 I3，更新 README 与 `docs/verification.md`。

- 验收：A2
- 范围：src、README.md、docs/verification.md
- 前置任务：无

## 任务 T3：端口核对只认实际监听
<!-- tw-section id=T3 type=task depends=scope-global,I4 -->
第三段。起点：看板刚停止、端口上只剩 TIME_WAIT 连接；终点：立即运行 `sop-board check` 第 2 步通过，确有进程监听时仍失败并列出进程。实现 I4，必要时更新 README 与 `docs/verification.md`。

- 验收：A3
- 范围：src、README.md、docs/verification.md
- 前置任务：无

## 任务 T4：Jira 搭建引导与内网信息
<!-- tw-section id=T4 type=task depends=scope-global,I5,I6 -->
第四段。起点：开发者请代理按指引搭建，团队使用 Jira Cloud；终点：开发者自行设好两个变量后，看板读到测试单据标题；内网页面看不到任何本机路径。实现 I5、I6，更新 `guide.md`、README 与 `docs/verification.md`。

- 验收：A4
- 范围：src、README.md、docs/verification.md
- 前置任务：无
