# 运行 #$run：经真实入口执行 SOP 场景

运行目录：$directory（检出 $checkout）

$verify_note

先读 brief.md，再读项目验证技能 $skill 及它引用的产品真实验证手册。预期只来自 scenarios.yaml；手册与技能说明怎样操作、观察和留证。

## 材料

- probes.jsonl 记录准备和即时探测。brief.md 列出准备时已登记为未执行的场景。在 scenarios.yaml 的可执行范围内写结果。

- scenarios.yaml：本轮场景原文（key 为 `<文件>#<编号>`，reason 为本轮原因）。
- pending.json：范围与原因；entries.json：入口声明与本检出的入口地址；repos.json：本检出各仓库的目录。
- services.json：项目声明了服务时，准备时各服务的状态（运行中、已停止、本机未搭建、异常退出、未上报）与业务说明；brief.md 的“服务”一节是它的摘要。
- run.env：入口地址变量，以及 Web 场景要用的 PW_CDP_ENDPOINT、PW_IDENTITIES、PLAYWRIGHT_BROWSERS_PATH。执行命令前 `set -a; . ./run.env; set +a`。

环境由看板守护进程按本检出启动，adapter 输出就绪即可执行，不一定每个服务都在运行。环境只经 sop-board 命令改变：场景起点要求某个依赖不可用时，用 `sop-board env stop <服务>` 停、`sop-board env start <服务>` 启（只对声明为可停的服务，停启记进本轮运行，提交时还停着的自动恢复）；进程、配置、adapter 脚本与仓库文件保持原样。场景起点用业务语言写依赖，按服务的业务说明对应到服务名。

看板可能在执行中途经 `sop-board restart` 换守护进程：浏览器、租约与本轮运行都保留，环境中断几十秒后重新就绪，运行摘要会记下这段中断。入口暂时不应答时运行 `sop-board env status`，等环境就绪后从头重做受影响的场景，按重做的结果写结果。

本轮每条场景都经真实入口实际执行，并保存新证据。

## 分组走一趟

场景用业务语言写，你像真实用户一样体验产品：进到一个页面，就把本组与这个页面有关的预期一起核对。

1. **分组**：先通读 scenarios.yaml，把起点相同、身份相同、经过同一页面的场景分成一组。组内只看不改的排前，会改变状态的（指派、转交、关闭、改设置、删除）排后；一条场景的操作会破坏另一条的起点时，把它排到后面或分到别的组。
2. **开始**：`sop-board run begin <场景键> <场景键> …` 一次开始这一组。
3. **核对**：准备起点，经真实入口操作。到达每个页面时，把组内与这个页面有关的预期一起核对，按场景分别留证：证据放在 `evidence/<场景键，# 换成 __>/`，同一张截图可以复制给多条场景。先做的操作改变了组内后面场景的起点时，后面的场景重新满足起点再核对。
4. **裁决**：一条场景的预期全部核对完，或已确定判不了，就为它写结果行并 `run verdict`，不等整组结束。一条失败，组内其他场景照常核对。
5. **业务等待**：起点或预期要等真实时间（定时任务、等待若干分钟）的场景，先完成触发操作并记下时刻，等待期间做别的组，到点再回来核对。

## 结果

每条场景执行前已经 `run begin`（见上节）。得到结果后在 results.jsonl 为该场景写一行，立即运行 `sop-board run verdict <场景键>`。通过或失败当场登记，本轮保留原行、结果和证据。受阻、跳过、未执行先暂记，条件补齐后重新 begin、更新这条的唯一一行并重新裁决。

命令输出出现 `scenario-timeout`，说明已开始的场景连续超过上限没有新的裁决：已有真实可判定结果的先裁决；其余记下已取得的证据，判断不了的记 blocked，转向下一组。因预算用完而未做或未裁决的场景保持未裁决，启动器会按本次实际新裁决与剩余范围决定是否接续。仍有场景在进行而无法裁决时，保留现场并结束会话，供接续者还原。

- 字段：scenario（场景 key）、status（pass/fail/blocked/skip/not-executed）、actual、executed_at（带时区的 ISO 8601，实际执行的时刻）、evidence（evidence/ 下的相对路径数组），可选 finding、note，原因字段见下文；其他字段会被拒绝。
- 实际执行的 pass、fail 至少有一份真实证据，放在 evidence/。blocked、skip、not-executed 必须写 finding。
- 判定只依据真实入口的观察；不能只看源码或地图就判通过。

执行不了的场景按性质写，不看发生在开始前还是执行中：

- **not-executed**（未执行：本机暂时不具备，补齐或换一台机器就能执行）：场景用到的服务本机未搭建或被人停着；缺少入口声明的凭据变量；等人处理的事，即真实账号或未声明账号的登录、人机验证、只发到使用者本人设备的验证码、身份使用次数到上限。写 scenario、status、cause、finding，不附证据。未执行不登记结果、不改变场景状态；是否自动入选取决于有无处置或卡住。
- **blocked**（受阻：意外故障，或全队都不具备的能力）：服务或浏览器异常退出（finding 附日志末尾）；前置数据准备失败；证据丢失；出现 browser may not be secure；入口或观察能力还没建设；需要停外部共享中间件或不可停服务的故障场景。finding 写明原因。
- **入口整体不可用**：同一个入口看起来对任何操作都失败时，先运行 `sop-board env probe <入口>`，结合真实操作及日志判断。连续挡住 2 条场景时，中止这个入口的其余场景。这 2 条写 blocked、cause: environment，finding 附现象与日志末尾。其余写 not-executed、同类别，finding 写“入口 <名称> 不可用，本轮中止”。用 `sop-board attention add --kind 环境问题 --run $run --text <入口与现象>` 登记一条。别的入口的场景照常执行，最后照常提交。修复复验不接受 not-executed：保留已裁决项，有条件的场景完成裁决，欠条件的场景留作未裁决。没有完整复验时回报现象。
- 中途停止本轮时，因以上阻点未做的场景写 not-executed，cause 与 finding 按实际阻点填写。

blocked 与 not-executed 必填 `cause`，以实际阻点选用：

| 类别 | 何时使用 |
| --- | --- |
| `environment` | 本机缺服务、服务被人为停着或缺凭据变量记未执行。服务或浏览器异常退出、证据丢失或 browser may not be secure 记受阻 |
| `precondition` | 前置数据准备失败，记受阻。拿不准是前置造不出还是产品缺陷时，给受阻行加 `"suspect_defect": true`，finding 写已核对到的现象 |
| `dependency` | 外部依赖在执行中意外故障记受阻，本机暂缺该依赖记未执行 |
| `scenario` | 场景起点、操作或预期有疑义，无法裁决，记受阻 |
| `capability` | 全队缺入口、观察通道或模拟故障的能力，记受阻 |
| `human` | 等人处理记未执行，finding 写需要人处理什么。提交自动生成待人处理，身份计数超限告警由看板登记 |

pass、fail 和 skip 沿用原字段。疑似缺陷只在 blocked 行标记，值为 JSON 的 true。

## 依赖故障的场景

起点要求某个依赖不可用（例如“库存查询不可用”）时：

1. 按 brief.md“服务”一节的业务说明找到对应的服务，确认它可停且正在运行。
2. `sop-board env stop <服务>`：命令等服务真正停下才返回，停启记进 run.env 里 SOP_BOARD_RUN 指的运行。失败时按返回的 error_code 处理（service-not-stoppable 写 blocked/capability；service-timeout 写 blocked/environment，finding 写实际超时；env-shared 写 not-executed/environment，finding 写明是哪些运行）。
3. 经真实入口执行场景，executed_at 写停着期间的实际执行时刻。结果详情会显示“执行时 <服务> 已停止”。
4. 写好结果后 `sop-board env start <服务>`；忘了也没关系，提交时自动恢复并记录。

对应的服务本机未搭建时写 not-executed、cause: environment。

## 凭据

入口 credential_env 只列变量名，取值在本会话环境中。声明的测试账号声明即授权：未登录时用这些变量直接登录、申请并从声明的测试邮箱读取验证码，不另向使用者要授权。取值只用于声明它的入口；不打印、不写入脚本、截图、结果或日志；证据先脱敏。缺少入口声明的凭据变量时，涉及该入口的场景写 not-executed、cause: environment，finding 只写变量名（“缺少凭据变量 <变量名>”）。

## Web 场景

按 $web_skill 执行：在运行目录用模板经 PW_CDP_ENDPOINT 连接看板启动的 Chrome for Testing（不自己启动浏览器、不运行 playwright install），workers: 1、retries: 0。录屏与 trace 开关已在 scope.json 按场景固定。报告用 `sop-board playwright convert` 转换后合并进 results.jsonl。给转换所得的 blocked 和 not-executed 行按上表补齐 cause，然后按场景逐条裁决。用一次 spec 执行一组时，运行前先 `run begin` 整组。

## 完成

完成本轮可做的场景后运行：

    sop-board run submit --run $run [--implementer <实现者会话编号> | --implemented-by <实现者的操作者>]

首次裁决或提交时，按任务里的说明声明实现者：会话编号用 `--implementer`；实现者是别的机器上的另一位操作者用 `--implemented-by`；任务没说明就都不填。首次裁决成功后，本轮沿用这个声明。结论裁决或提交时共享库连不上（`database-unreachable`）时运行保持 prepared、结果留在运行目录，报告后等恢复，用同一行和证据重试原命令，只会登记一次。认证失败（`database-authentication`）时按 sop-board 技能的 references/shared.md 给出本机设置页地址，请使用者改密码，再重试原命令；会话不读取、不输出 config.yaml 的 shared_database.password，也不经手密码。

裁决或提交返回 `database-version` 时，按 sop-board 技能的 references/shared.md「业务记录库升级」处理。运行保持 prepared，结果留在运行目录，恢复后重试原命令。

提交登记暂记裁决及没有裁决的完整结果行，汇总登记、作废（运行期间场景内容变了）、未执行及未裁决的场景，以及停启记录、自动恢复的服务和运行期间异常退出的服务。不要用其他方式登记结果。

转述登记清单时，逐条带出 `recorded[].note` 中的保留意见。
