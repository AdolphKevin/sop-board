# 运行 #$run：经真实入口执行 SOP 场景

运行目录：$directory（检出 $checkout）

$verify_note

先读 brief.md，再读项目验证技能 $skill 及它引用的产品真实验证手册。预期只来自 scenarios.yaml；手册与技能说明怎样操作、观察和留证。

## 材料

- scenarios.yaml：本轮场景原文（key 为 `<文件>#<编号>`，reason 为本轮原因）。
- pending.json：范围与原因；entries.json：入口声明与本检出的入口地址；repos.json：本检出各仓库的目录。
- services.json：项目声明了服务时，准备时各服务的状态（运行中、已停止、本机未搭建、异常退出、未上报）与业务说明；brief.md 的“服务”一节是它的摘要。
- run.env：入口地址变量，以及 Web 场景要用的 PW_CDP_ENDPOINT、PW_IDENTITIES、PLAYWRIGHT_BROWSERS_PATH。执行命令前 `set -a; . ./run.env; set +a`。

环境由看板守护进程按本检出启动，adapter 输出就绪即可执行，不一定每个服务都在运行。环境只经 sop-board 命令改变：场景起点要求某个依赖不可用时，用 `sop-board env stop <服务>` 停、`sop-board env start <服务>` 启（只对声明为可停的服务，停启记进本轮运行，提交时还停着的自动恢复）；进程、配置、adapter 脚本与仓库文件保持原样。场景起点用业务语言写依赖，按服务的业务说明对应到服务名。

看板可能在执行中途经 `sop-board restart` 换守护进程：浏览器、租约与本轮运行都保留，环境中断几十秒后重新就绪，运行摘要会记下这段中断。入口暂时不应答时运行 `sop-board env status`，等环境就绪后从头重做受影响的场景，按重做的结果写结果。

本轮每条场景都经真实入口实际执行，并保存新证据。

## 结果

在运行目录写 results.jsonl，每个范围内场景恰好一行，每执行完一个就写一行：

- 字段：scenario（场景 key）、status（pass/fail/blocked/skip/not-executed）、actual、executed_at（带时区的 ISO 8601，实际执行的时刻）、evidence（evidence/ 下的相对路径数组），可选 finding、note；其他字段会被拒绝。
- 实际执行的 pass、fail 至少有一份真实证据，放在 evidence/。blocked、skip、not-executed 必须写 finding。
- 判定只依据真实入口的观察；不能只看源码或地图就判通过。

执行不了的场景按性质写，不看发生在开始前还是执行中：

- **not-executed**（未执行：本机暂时不具备，补齐或换一台机器就能执行）：场景用到的服务本机未搭建或被人停着；缺少入口声明的凭据变量；等人处理的事，即真实账号或未声明账号的登录、人机验证、只发到使用者本人设备的验证码、身份使用次数到上限。写 `{"scenario", "status": "not-executed", "finding": 缺什么}`，不附证据。等人处理的事另用 `sop-board attention add` 登记（identity-limit 已自动登记）。未执行不登记结果、不改变场景状态，场景留在待执行范围。
- **blocked**（受阻：意外故障，或全队都不具备的能力）：服务或浏览器异常退出（finding 附日志末尾，并 `sop-board attention add --kind 环境问题` 登记）；前置数据准备失败；证据丢失；出现 browser may not be secure；入口或观察能力还没建设；需要停外部共享中间件或不可停服务的故障场景。finding 写明原因。
- 中途停止本轮时，没执行的场景写 not-executed，finding 写停止的原因。

## 依赖故障的场景

起点要求某个依赖不可用（例如“库存查询不可用”）时：

1. 按 brief.md“服务”一节的业务说明找到对应的服务，确认它可停且正在运行。
2. `sop-board env stop <服务>`：命令等服务真正停下才返回，停启记进 run.env 里 SOP_BOARD_RUN 指的运行。失败时按返回的 error_code 处理（service-not-stoppable 写 blocked；service-timeout 如实写进 finding 后写 blocked；env-shared 说明这套环境上还有别的运行，写 not-executed，finding 写明是哪些运行）。
3. 经真实入口执行场景，executed_at 写停着期间的实际执行时刻。结果详情会显示“执行时 <服务> 已停止”。
4. 写好结果后 `sop-board env start <服务>`；忘了也没关系，提交时自动恢复并记录。

对应的服务本机未搭建时写 not-executed。

## 凭据

入口 credential_env 只列变量名，取值在本会话环境中。声明的测试账号声明即授权：未登录时用这些变量直接登录、申请并从声明的测试邮箱读取验证码，不另向使用者要授权。取值只用于声明它的入口；不打印、不写入脚本、截图、结果或日志；证据先脱敏。缺少入口声明的凭据变量时，涉及该入口的场景写 not-executed，finding 只写变量名（“缺少凭据变量 <变量名>”）。

## Web 场景

按 $web_skill 执行：在运行目录用模板经 PW_CDP_ENDPOINT 连接看板启动的 Chrome for Testing（不自己启动浏览器、不运行 playwright install），workers: 1、retries: 0。录屏与 trace 开关已在 scope.json 按场景固定。报告用 `sop-board playwright convert` 转换后合并进 results.jsonl。

## 完成

确认 results.jsonl 齐全后运行：

    sop-board run submit --run $run [--implementer <实现者会话编号> | --implemented-by <实现者的操作者>]

实现者按任务里的说明填：会话编号用 `--implementer`；实现者是别的机器上的另一位操作者用 `--implemented-by`；任务没说明就都不填。提交时共享库连不上（`database-unreachable`）时运行保持 prepared、结果留在运行目录，报告后等恢复再原样重新提交，只会登记一次。认证失败（`database-authentication`）时按 sop-board 技能的 references/shared.md 给出本机设置页地址，请使用者改密码，再原样重新提交；会话不读取、不输出 config.yaml 的 shared_database.password，也不经手密码。

提交输出会列出登记、作废（运行期间场景内容变了）与未执行的场景，以及停启记录、自动恢复的服务和运行期间异常退出的服务。不要用其他方式登记结果。
