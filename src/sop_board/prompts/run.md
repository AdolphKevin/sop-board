# 运行 #$run：经真实入口执行 SOP 场景

运行目录：$directory（检出 $checkout）

$verify_note

先读 brief.md，再读项目验证技能 $skill 及它引用的产品真实验证手册。预期只来自 scenarios.yaml；手册与技能说明怎样操作、观察和留证。

## 材料

- scenarios.yaml：本轮场景原文（key 为 `<文件>#<编号>`，reason 为本轮原因）。
- pending.json：范围与原因；entries.json：入口声明与本检出的入口地址；repos.json：本检出各仓库的目录。
- run.env：入口地址变量，以及 Web 场景要用的 PW_CDP_ENDPOINT、PW_IDENTITIES、PLAYWRIGHT_BROWSERS_PATH。执行命令前 `set -a; . ./run.env; set +a`。
- 产品变化待判定的场景另有 previous-results.json 与 changes.json（见下文）。

环境由看板守护进程按本检出启动，已经就绪；不要启动、停止或替换产品环境，也不要改动任何仓库中的文件。

## 产品变化待判定（reason 为 source-changed）

依据 changes.json 的完整改动范围（各仓自上次通过以来的提交差异与当前未提交改动）、场景内容与依赖关系判断每条场景是否受影响。previous-results.json 中带 product_changed 的通过，执行时产品处于变化中：场景依赖所列任一路径时，按无法证明不受影响处理。

- 受影响，或无法证明不受影响：经真实入口重新执行。
- 能证明不受影响：保留原通过，不再执行。结果行写 `"status": "pass", "kept": true`，note 写明改动范围、依赖关系与不受影响的理由；evidence 可以为空，看板保留原证据。

在运行目录写 impact.json：数组，恰好覆盖全部 source-changed 场景，每项 `{"scenario", "affected", "reason"}`。affected 为 false 的对应 kept 结果，为 true 的对应实际执行结果。

其他原因的场景（未执行、上次不通过、须实际执行）都要实际执行并保存新证据。

## 结果

在运行目录写 results.jsonl，每个范围内场景恰好一行，每执行完一个就写一行：

- 字段：scenario（场景 key）、status（pass/fail/blocked/skip/not-executed）、actual、executed_at（带时区的 ISO 8601）、evidence（evidence/ 下的相对路径数组），可选 finding、note、kept。
- 实际执行的 pass、fail 至少有一份真实证据，放在 evidence/。blocked、skip 必须写 finding。
- 中途停止时，没执行的场景写 `{"scenario", "status": "not-executed", "finding": 原因}`，不附证据；它们留在待执行范围。
- 判定只依据真实入口的观察；不能只看源码或地图就判通过。凭据缺失、前置数据准备不了、只有人能做的事（真实账号登录、人机验证、只发到使用者本人设备的验证码），记 blocked 并写明缺什么（需要人做的事用 `sop-board attention add` 登记）。

## 凭据

入口 credential_env 只列变量名，取值在本会话环境中。声明的测试账号声明即授权：未登录时用这些变量直接登录、申请并从声明的测试邮箱读取验证码，不另向使用者要授权。取值只用于声明它的入口；不打印、不写入脚本、截图、结果或日志；证据先脱敏。缺变量的场景记 blocked，finding 只写变量名。

## Web 场景

按 $web_skill 执行：在运行目录用模板经 PW_CDP_ENDPOINT 连接看板启动的 Chrome for Testing（不自己启动浏览器、不运行 playwright install），workers: 1、retries: 0。录屏与 trace 开关已在 scope.json 按场景固定。报告用 `sop-board playwright convert` 转换后合并进 results.jsonl。

## 完成

确认 results.jsonl（及需要时的 impact.json）齐全后运行：

    sop-board run submit --run $run [--implementer <实现者会话编号>]

提交输出会列出登记、作废（运行期间场景内容变了）与未执行的场景。不要用其他方式登记结果。
